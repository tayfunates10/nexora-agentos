import { expect, type Page } from "@playwright/test";

/**
 * The persistent sidebar is the navigation under test. A module name also appears on the
 * control-centre card, so a plain name lookup would match twice; this scopes it.
 */
export function sidebarLink(page: Page, name: string) {
  return page.locator(".sidebar").getByRole("link", { name, exact: true });
}

/**
 * Scroll the page once so every reveal-on-scroll section has been observed, then prove
 * none is still hidden. A capture taken without this would show an empty band where a
 * section below the fold sits, and a section that never reveals would go unnoticed.
 */
export async function settleReveals(page: Page) {
  // The observer is attached when the client component mounts, so a scroll that happens
  // before hydration reveals nothing. Retrying the pass removes that race instead of
  // hiding it behind a fixed wait.
  await expect(async () => {
    await page.evaluate(async () => {
      for (let y = 0; y < document.body.scrollHeight; y += window.innerHeight) {
        window.scrollTo(0, y);
        await new Promise(resolve => requestAnimationFrame(resolve));
      }
      window.scrollTo(0, 0);
    });
    expect(await page.locator(".reveal:not(.is-visible)").count()).toBe(0);
  }).toPass({ timeout: 15_000 });
}

/** Nothing may overflow the viewport horizontally, at any width the product claims. */
export async function expectNoHorizontalOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
}
