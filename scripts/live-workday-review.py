"""Exercise the real Workday engine in an isolated window, stopping at Review."""
import argparse
import asyncio
import json
import socket
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_use import BrowserSession
from playwright.async_api import async_playwright
from backend.core.config import load_profile
from backend.core.shared_config import RESUME_PATH
from backend.core.autofill_facts import load_autofill_facts
from backend.core.workday_flow import run_workday_deterministic, _current_page
from cli.workday_pages import _probe


async def main(args):
    host = urlsplit(args.url).hostname
    async with async_playwright() as p:
        source = await p.chromium.connect_over_cdp(args.source_cdp)
        cookies = [c for c in await source.contexts[0].cookies(args.url)
                   if host == c['domain'].lstrip('.') or host.endswith('.' + c['domain'].lstrip('.'))]
        await source.close()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        context = await p.chromium.launch_persistent_context(
            tempfile.mkdtemp(prefix='langhire-workday-test-'), headless=False,
            args=[f'--remote-debugging-port={port}'],
        )
        await context.add_cookies(cookies)
        page = context.pages[0]
        await page.goto(args.url, wait_until='domcontentloaded')
        await page.wait_for_selector('[data-automation-id="pageFooterNextButton"]', timeout=30000)
        await page.wait_for_selector('input[aria-required="true"]', state='visible', timeout=30000)
        browser = BrowserSession(cdp_url=f'http://127.0.0.1:{port}', keep_alive=True)
        await browser.start()
        print(f'LIVE_TEST_CDP http://127.0.0.1:{port}', flush=True)
        profile = load_profile()
        facts = load_autofill_facts(profile, RESUME_PATH)
        facts.update(job_title=await page.title(), job_company='WEX')
        task = asyncio.create_task(run_workday_deterministic(
            browser, facts=facts, resume_path=RESUME_PATH, worker_id=1,
            passes=12, llm_cleanup=True, llm_steps=60, llm_timeout=300,
            profile=profile,
        ))
        previous = None
        while not task.done():
            state = await _probe(await _current_page(browser))
            brief = {k: state.get(k) for k in ('active', 'review', 'blockers', 'errors', 'safeNext')}
            if brief != previous:
                print('LIVE_STEP ' + json.dumps(brief), flush=True)
                previous = brief
            await asyncio.wait({task}, timeout=5)
        result = await task
        final = await _probe(await _current_page(browser))
        print('LIVE_RESULT ' + json.dumps({
            'reached_review': final.get('review'), 'active': final.get('active'),
            'blockers': final.get('blockers'), 'errors': final.get('errors'),
            'engine_blockers': result.get('blockers'),
            'llm': result.get('summary', {}).get('llm_cleanup'),
        }), flush=True)
        if not final.get('review'):
            # Keep a failed page available briefly for diagnosis.
            await asyncio.sleep(300)
        await context.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-cdp', required=True)
    parser.add_argument('--url', required=True)
    asyncio.run(main(parser.parse_args()))
