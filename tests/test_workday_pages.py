import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from cli.workday_pages import is_workday_application, run_workday_pages, step_script


class WorkdayPagesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.chromium = await self.playwright.chromium.launch(headless=True)
        self.page = await self.chromium.new_page()
        await self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<main></main>"))
        await self.page.goto("https://example.wd1.myworkdayjobs.com/en-US/jobs/job/Engineer_1/apply/applyManually")
        self.browser = AsyncMock()
        self.browser.get_current_page.return_value = self.page
        self.pause_patch = patch("cli.workday_pages.wait_while_ai_paused", new=AsyncMock())
        self.pause = self.pause_patch.start()
        self.release_patch = patch("cli.workday_pages.release_review_handoff", new=AsyncMock())
        self.release = self.release_patch.start()
        await self.page.set_content('''
          <main>
            <h2>My Information</h2>
            <form><label>Name<input name="name" required></label>
              <button type="button" data-automation-id="pageFooterNextButton">Save and Continue</button>
            </form>
          </main>
        ''')

    async def asyncTearDown(self):
        self.release_patch.stop()
        self.pause_patch.stop()
        await self.chromium.close()
        await self.playwright.stop()

    async def filled(self):
        await self.page.locator('input').fill('Test candidate')
        return {"verified": True, "clicked": True, "filled_by_fapply": 1}

    async def run_flow(self, fill=None, **kwargs):
        return await run_workday_pages(self.browser, fill or self.filled,
                                       poll_interval=0.005, transition_timeout=0.15, **kwargs)

    async def test_fills_each_spa_step_and_stops_before_submit(self):
        await self.page.evaluate('''() => {
          window.nextClicks = 0; window.submits = 0;
          document.querySelector('button').onclick = e => {
            if (e.target.textContent === 'Submit') { window.submits++; return; }
            window.nextClicks++;
            document.querySelector('h2').textContent = window.nextClicks === 1 ? 'My Experience' : 'Review';
            document.querySelector('input').value = '';
            if (window.nextClicks === 2) e.target.textContent = 'Submit';
          };
        }''')
        fill = AsyncMock(side_effect=self.filled)
        result = await self.run_flow(fill)
        self.assertTrue(result['reached_review'])
        self.assertEqual(fill.await_count, 2)
        self.assertEqual(len(result['pages']), 2)
        self.assertEqual(result['filled_by_fapply'], 2)
        self.assertEqual(await self.page.evaluate('window.nextClicks'), 2)
        self.assertEqual(await self.page.evaluate('window.submits'), 0)
        self.release.assert_awaited_once()

    async def test_missing_required_field_blocks_next(self):
        fill = AsyncMock(return_value={"verified": True, "filled_by_fapply": 1})
        result = await self.run_flow(fill)
        self.assertFalse(result['reached_review'])
        self.assertIn('needs your input', result['reason'])
        self.assertIn('Name', result['pages'][0]['blockers'])

    async def test_added_experience_rows_are_not_treated_as_new_pages(self):
        async def fill():
            await self.page.locator('form').evaluate("e => e.insertAdjacentHTML('afterbegin', '<input name=experience value=intern>')")
            await self.page.locator('input[name=name]').fill('Test candidate')
            return {"verified": True, "clicked": True, "filled_by_fapply": 2}
        await self.page.locator('button').evaluate("e => e.onclick=()=>document.querySelector('h2').textContent='Review'")
        mock_fill = AsyncMock(side_effect=fill)
        result = await self.run_flow(mock_fill)
        self.assertTrue(result['reached_review'])
        mock_fill.assert_awaited_once()

    async def test_paused_page_never_advances(self):
        await self.filled()
        await self.page.evaluate("() => { window.__LANGHIRE_PAUSE_CONTROL = {paused:true}; }")
        self.assertFalse((await self.page.evaluate(step_script(advance=True)))['clicked'])

    async def test_site_validation_error_stops_after_one_next_click(self):
        await self.page.evaluate('''() => {
          window.nextClicks = 0;
          document.querySelector('button').onclick = () => {
            window.nextClicks++;
            const error = document.createElement('div'); error.role = 'alert';
            error.textContent = 'Choose a school'; document.body.appendChild(error);
          };
        }''')
        result = await self.run_flow()
        self.assertIn('Choose a school', result['reason'])
        self.assertEqual(await self.page.evaluate('window.nextClicks'), 1)

    async def test_unchanged_page_does_not_click_next_repeatedly(self):
        await self.page.evaluate("() => {window.clicks=0; document.querySelector('button').onclick=()=>window.clicks++;}")
        result = await self.run_flow()
        self.assertIn('did not advance', result['reason'])
        self.assertEqual(await self.page.evaluate('window.clicks'), 1)

    async def test_review_and_final_control_are_never_clicked(self):
        await self.page.locator('h2').evaluate("e => e.textContent = 'Review'")
        fill = AsyncMock()
        result = await self.run_flow(fill)
        self.assertTrue(result['reached_review'])
        fill.assert_not_awaited()
        # A last-moment footer relabel must also be caught at click time.
        await self.page.locator('h2').evaluate("e => e.textContent = 'Application'")
        await self.page.locator('button').evaluate("e => e.textContent = 'Submit'")
        self.assertFalse((await self.page.evaluate(step_script(advance=True)))['clicked'])

    async def test_prefilled_page_can_advance_after_zero_change_completion(self):
        await self.filled()
        await self.page.locator('button').evaluate("e => e.onclick=()=>document.querySelector('h2').textContent='Review'")
        fill = AsyncMock(return_value={"verified": False, "clicked": True, "event": {"filledCount": 0}})
        result = await self.run_flow(fill)
        self.assertTrue(result['reached_review'])

    async def test_cancel_after_fill_prevents_next(self):
        flag = {}
        async def fill():
            result = await self.filled()
            flag['cancel_requested'] = True
            return result
        result = await self.run_flow(fill, cancel_flag=flag)
        self.assertIn('Queue stopped', result['reason'])
        self.assertEqual(await self.page.locator('h2').inner_text(), 'My Information')

    async def test_unresolved_custom_required_choice_blocks_next(self):
        await self.page.locator('form').evaluate('''e => e.insertAdjacentHTML('afterbegin',
          '<button type="button" aria-haspopup="listbox" aria-required="true" aria-label="Country">Select One</button>')''')
        result = await self.run_flow()
        self.assertIn('Country', result['reason'])

    async def test_verification_page_is_handed_to_user_without_fill(self):
        await self.page.locator('input').evaluate("e => e.setAttribute('aria-label', 'Verification code')")
        fill = AsyncMock()
        result = await self.run_flow(fill)
        self.assertIn('verification', result['reason'])
        fill.assert_not_awaited()

    async def test_always_releases_controls_after_fill_error(self):
        with self.assertRaisesRegex(RuntimeError, 'extension error'):
            await self.run_flow(AsyncMock(side_effect=RuntimeError('extension error')))
        self.release.assert_awaited_once()


def test_workday_host_matches_only_actual_domains():
    assert is_workday_application('https://amgen.wd1.myworkdayjobs.com/job/1/apply')
    assert not is_workday_application('https://notmyworkdayjobs.com/job/1/apply')
    assert not is_workday_application('https://jobs.example.com/?next=myworkdayjobs.com')
