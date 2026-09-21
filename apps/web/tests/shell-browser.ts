import { expect, type BrowserContext, type Page } from "@playwright/test";
import { EN, TR } from "./ui-text.ts";
import { expectNoHorizontalOverflow, settleReveals } from "./shell.ts";

/**
 * The application frame itself: the language a browser is served, the language it can
 * choose, the theme, the narrow-screen drawer and the widths the product claims to work
 * at. Language and theme are independent, so all four combinations are exercised.
 */
export async function checkShell(page: Page, context: BrowserContext, detailUrl: string) {
  // ---------------------------------------------------------------- language
  await page.goto(detailUrl);
  await expect(page.locator("html")).toHaveAttribute("lang", "tr");
  await expect(page.getByRole("heading", { name: TR.t("hub.title") })).toBeVisible();

  // Switching keeps the reader on the same page, in the same place.
  await page.getByRole("button", { name: "English" }).first().click();
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.getByRole("heading", { name: EN.t("hub.title") })).toBeVisible();
  expect(page.url()).toBe(detailUrl);
  const stored = (await context.cookies()).find(cookie => cookie.name === "nexora_locale");
  expect(stored?.value).toBe("en");
  expect(stored?.httpOnly).toBe(false);

  // A filtered list keeps its filter and its cursor across the switch.
  await page.goto(`${detailUrl}/runs?status=cancelled`);
  await expect(page.getByRole("link", { name: EN.t("runs.status.cancelled"), exact: true }))
    .toHaveAttribute("aria-current", "page");
  await page.getByRole("button", { name: "Türkçe" }).first().click();
  await expect(page.locator("html")).toHaveAttribute("lang", "tr");
  expect(page.url()).toBe(`${detailUrl}/runs?status=cancelled`);
  await expect(page.getByRole("link", { name: TR.t("runs.status.cancelled"), exact: true }))
    .toHaveAttribute("aria-current", "page");

  // Text typed into a form is not thrown away by changing the language.
  await page.goto(`${detailUrl}/knowledge`);
  await page.getByLabel(TR.t("knowledge.titleLabel")).fill("Taslak başlık");
  await page.getByRole("button", { name: "English" }).first().click();
  await expect(page.getByLabel(EN.t("knowledge.titleLabel"))).toHaveValue("Taslak başlık");
  await page.getByRole("button", { name: "Türkçe" }).first().click();

  // ------------------------------------------------------------------- theme
  await page.goto(detailUrl);
  // "System" stores a choice but renders no attribute, leaving it to the stylesheet.
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", /.+/);
  await page.getByRole("button", { name: TR.t("theme.dark") }).first().click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  // The theme survives a language change, and the language survives a theme change.
  await page.getByRole("button", { name: "English" }).first().click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await page.getByRole("button", { name: EN.t("theme.light") }).first().click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await page.getByRole("button", { name: "Türkçe" }).first().click();

  // Both themes are captured at both widths. The control lives in the sidebar, which a
  // narrow viewport replaces with the drawer, so the choice is made at desktop width.
  for (const theme of ["dark", "light"] as const) {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(detailUrl);
    await page.getByRole("button", { name: TR.t(`theme.${theme}`) }).first().click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(detailUrl);
      await expect(page.getByRole("heading", { name: TR.t("hub.title") })).toBeVisible();
      await expectNoHorizontalOverflow(page);
      await settleReveals(page);
    await page.screenshot({ path: `/tmp/nexora-hub-tr-${theme}-${width}.png`, fullPage: true, animations: "disabled" });
    }
  }
  await page.setViewportSize({ width: 1440, height: 900 });

  // ------------------------------------------------------- narrow-screen menu
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(detailUrl);
  const hamburger = page.getByRole("button", { name: TR.t("navigation.openMenu") });
  await expect(hamburger).toBeVisible();
  await hamburger.click();
  const drawer = page.getByRole("dialog", { name: TR.t("navigation.workspaceMenu") });
  await expect(drawer).toBeVisible();
  // Every module stays reachable from the drawer, not only the ones a preview showed.
  for (const key of [
    "navigation.agents", "navigation.runs", "navigation.approvals", "navigation.tools",
    "navigation.knowledge", "navigation.evaluations", "navigation.spend",
  ] as const) {
    await expect(drawer.getByRole("link", { name: TR.t(key), exact: true })).toHaveCount(1);
  }
  // Escape closes it and hands focus back to the control that opened it.
  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);
  await expect(hamburger).toBeFocused();
  await hamburger.click();
  await drawer.getByRole("link", { name: TR.t("navigation.spend"), exact: true }).click();
  await expect(page).toHaveURL(`${detailUrl}/spend`);
  await expect(page.getByRole("dialog")).toHaveCount(0);

  // ---------------------------------------------------------- narrowest width
  await page.setViewportSize({ width: 320, height: 740 });
  for (const path of ["", "/runs", "/knowledge", "/spend", "/tools", "/approvals"]) {
    await page.goto(detailUrl + path);
    await expectNoHorizontalOverflow(page);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(detailUrl);
}

/** The pages that exist before sign-in carry the same controls. */
export async function checkPublicShell(page: Page, origin: string) {
  await page.goto(origin + "/");
  await expect(page.locator("html")).toHaveAttribute("lang", "tr");
  await expect(page.getByRole("heading", { name: TR.t("landing.titleAccent") })).toBeVisible();
  await expect(page.getByRole("heading", { name: TR.t("health.title") })).toBeVisible();
  await page.getByRole("button", { name: "English" }).first().click();
  await expect(page.getByRole("heading", { name: EN.t("landing.titleAccent") })).toBeVisible();

  await page.goto(origin + "/login");
  await expect(page.getByRole("heading", { name: EN.t("auth.title") })).toBeVisible();
  await page.getByRole("button", { name: "Türkçe" }).first().click();
  await expect(page.getByRole("heading", { name: TR.t("auth.title") })).toBeVisible();
  await expect(page.getByRole("button", { name: TR.t("auth.continue") })).toBeVisible();

  for (const theme of ["dark", "light"] as const) {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(origin + "/");
    await page.getByRole("button", { name: TR.t(`theme.${theme}`) }).first().click();
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(origin + "/");
      await expectNoHorizontalOverflow(page);
      await settleReveals(page);
      await page.screenshot({
        path: `/tmp/nexora-landing-tr-${theme}-${width}.png`, fullPage: true,
        animations: "disabled",
      });
      await page.goto(origin + "/login");
      await expectNoHorizontalOverflow(page);
    }
  }
  await page.setViewportSize({ width: 1440, height: 900 });
}

/**
 * A browser that states a language and has no stored choice is served that language. The
 * check runs in its own context so it cannot inherit the cookie set above.
 */
export async function checkLanguageNegotiation(context: BrowserContext, origin: string) {
  const english = await context.browser()!.newContext({ locale: "en-GB" });
  try {
    const page = await english.newPage();
    await page.goto(origin + "/login");
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    await expect(page.getByRole("heading", { name: EN.t("auth.title") })).toBeVisible();
  } finally { await english.close(); }

  const unsupported = await context.browser()!.newContext({ locale: "de-DE" });
  try {
    const page = await unsupported.newPage();
    await page.goto(origin + "/login");
    // Nothing this platform serves was asked for, so the default language answers.
    await expect(page.locator("html")).toHaveAttribute("lang", "tr");
  } finally { await unsupported.close(); }
}
