import type { Ui } from "../../lib/i18n/messages.ts";
import type { Theme } from "../../lib/i18n/theme.ts";
import { BrandMark } from "../ui/Icon.tsx";
import { LanguageControl } from "./LanguageControl.tsx";
import { ThemeControl } from "./ThemeControl.tsx";

/**
 * The frame for the two pages that exist before sign-in. There is no workspace navigation
 * here, but the theme and language controls are, so a reader can set both before they
 * have an account context.
 */
export function PublicShell({ ui, theme, actions, children }: {
  ui: Ui; theme: Theme; actions?: React.ReactNode; children: React.ReactNode;
}) {
  return <div className="public-shell">
    <a className="skip-link" href="#main-content">{ui.t("navigation.skipToContent")}</a>
    <header className="public-header">
      <a className="brand" href="/">
        <span className="brand-mark"><BrandMark size={19}/></span>
        <span>
          <span className="brand-name">{ui.t("common.brand")}</span><br/>
          <span className="brand-suffix">{ui.t("common.brandSuffix")}</span>
        </span>
      </a>
      <div className="public-header-actions">
        <LanguageControl label={ui.t("language.label")} current={ui.locale}/>
        <ThemeControl
          label={ui.t("theme.label")}
          current={theme}
          labels={{
            system: ui.t("theme.system"), light: ui.t("theme.light"), dark: ui.t("theme.dark"),
          }}
        />
        {actions}
      </div>
    </header>
    <main id="main-content" className="public-main route-enter">{children}</main>
    <footer className="public-footer">
      <span>{ui.t("common.productName")}</span>
      <span>{ui.t("landing.footerTagline")}</span>
    </footer>
  </div>;
}
