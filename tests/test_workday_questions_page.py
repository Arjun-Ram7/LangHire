"""fill_workday_questions against synthetic Workday-style widgets in a real (headless) Chromium."""
import unittest
from random import Random
from unittest.mock import patch

from playwright.async_api import async_playwright

from backend.core import workday_experience as we


class _Domain:
    def __init__(self, cdp, name):
        self.cdp, self.name = cdp, name

    def __getattr__(self, method):
        async def call(params=None, session_id=None):
            return await self.cdp.send(f"{self.name}.{method}", params or {})

        return call


class _Send:
    def __init__(self, cdp):
        self.cdp = cdp

    def __getattr__(self, name):
        return _Domain(self.cdp, name)


class PageBrowser:
    """The slice of a browser-use session the filler uses, over a Playwright CDP session."""

    def __init__(self, cdp):
        session = type("Session", (), {"cdp_client": type("Client", (), {"send": _Send(cdp)})(), "session_id": "x"})()
        self._session = session

    async def get_or_create_cdp_session(self, **_):
        return self._session


def listbox_field(field_id, question, options):
    items = "".join(f"'{o}'," for o in options)
    return f"""
    <div data-automation-id="formField-{field_id}" data-fkit-id="q--{field_id}"><fieldset><legend><p>{question}<abbr>*</abbr></p></legend>
      <button id="q--{field_id}" aria-haspopup="listbox" type="button">Select One</button></fieldset></div>
    <script>
      (() => {{
        const b = document.getElementById('q--{field_id}');
        b.onclick = () => {{
          const old = document.getElementById('lb-{field_id}'); if (old) {{ old.remove(); return; }}
          const ul = document.createElement('ul'); ul.id = 'lb-{field_id}'; ul.setAttribute('role', 'listbox');
          for (const t of ['Select One', {items}]) {{ const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = t; li.style.padding = '6px';
            li.onclick = () => {{ b.textContent = t; ul.remove(); }}; ul.appendChild(li); }}
          b.after(ul);
        }};
      }})();
    </script>"""


HTML = (
    "<body>"
    + listbox_field("plexusHear", "How Did You Hear About Us?", ["Facebook", "LinkedIn", "Indeed"])
    + listbox_field("vet", "Veteran Status", [
        "I identify as one or more of the classifications of protected veteran", "I am not a protected veteran",
        "I do not wish to self identify"])
    + listbox_field("worked", "Have you previously worked for Plexus?", ["Yes", "No"])
    + """
    <div data-automation-id="formField-source" data-fkit-id="source--source" style="margin-top:40px"><label>How Did You Hear About Us?<abbr>*</abbr></label>
      <div data-automation-id="multiSelectContainer"><input id="source--source" placeholder="Search">
        <div data-automation-id="promptAriaInstruction">0 items selected</div><div id="chosen"></div></div></div>
    <div id="msPopup" data-automation-id="activeListContainer" style="display:none; position:absolute; left:20px; width:300px"></div>
    <ul role="listbox" style="position:absolute; top:1500px; left:20px; width:300px; margin:0" id="stateList">
      <li role="option">Massachusetts</li><li role="option">Michigan</li></ul>
    <div data-automation-id="formField-sal" data-fkit-id="q--sal"><fieldset><legend><p>What is your base salary range expectations?<abbr>*</abbr></p></legend>
      <textarea id="q--sal"></textarea></fieldset></div>
    <script>
      (() => {
        const input = document.getElementById('source--source'), popup = document.getElementById('msPopup');
        input.addEventListener('click', () => {
          const r = input.getBoundingClientRect();
          popup.style.top = (window.scrollY + r.bottom + 4) + 'px'; popup.style.display = 'block';
          popup.innerHTML = ['Bracco Contact Me', 'Career Websites', 'Employee Referral', 'Internal', 'Job Fair/Event', 'Social Media']
            .map(t => `<div data-automation-id="promptOption" style="padding:6px">${t}</div>`).join('');
          for (const opt of popup.children) opt.onclick = () => {
            document.getElementById('chosen').textContent = opt.textContent; popup.style.display = 'none';
            document.querySelector('[data-automation-id=promptAriaInstruction]').textContent = '1 item selected';
          };
        });
      })();
    </script></body>"""
)

FACTS = {
    "veteran_status": "Not a veteran", "previously_worked_for_company": "no",
    "desired_pay": "Negotiable", "phone_country_code": "+1",
}


class QuestionWidgetTests(unittest.IsolatedAsyncioTestCase):
    async def fill(self, html, facts=FACTS):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1000, "height": 700})
            await page.set_content(html)
            cdp = await page.context.new_cdp_session(page)
            filled = await we.fill_workday_questions(PageBrowser(cdp), facts)
            state = await page.evaluate("""() => ({
              buttons: Object.fromEntries(Array.from(document.querySelectorAll('button')).map(b => [b.id, b.textContent])),
              chosen: document.getElementById('chosen')?.textContent || '',
              salary: document.getElementById('q--sal')?.value || '' })""")
            await browser.close()
        return filled, state

    async def test_dropdowns_veteran_and_previously_worked_and_the_button_hear_about_are_answered(self):
        filled, state = await self.fill(HTML)

        self.assertEqual(state["buttons"]["q--plexusHear"], "LinkedIn")
        self.assertEqual(state["buttons"]["q--vet"], "I am not a protected veteran")
        self.assertEqual(state["buttons"]["q--worked"], "No")
        self.assertGreaterEqual(filled, 3)

    async def test_the_search_style_hear_about_gets_a_harmless_option_from_its_own_list(self):
        with patch.object(we.random, "Random", return_value=Random(3)):
            _filled, state = await self.fill(HTML)

        # Not "Massachusetts" (another widget's list), and not a referral / internal / event option.
        self.assertIn(state["chosen"], {"Bracco Contact Me", "Career Websites", "Social Media"})

    async def test_free_text_salary_is_filled(self):
        _filled, state = await self.fill(HTML)

        self.assertEqual(state["salary"], "Negotiable")
