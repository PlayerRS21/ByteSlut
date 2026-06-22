/**
 * _validator.js — Theme CSS Coverage Validator
 * =============================================
 * Runs in the browser console when a non-default theme is active.
 * Checks that every required CSS class is styled by the loaded theme.
 * Called automatically if window.BYTESLUT_DEBUG = true.
 *
 * Usage in browser console:
 *   window.BYTESLUT_DEBUG = true; location.reload();
 *
 * Or call directly:
 *   ByteSlutThemeValidator.validate('glass');
 */

window.ByteSlutThemeValidator = (function() {

  // Every CSS class that a theme MUST style to be considered complete.
  // If a theme doesn't have a rule for one of these, the validator flags it.
  const REQUIRED_SELECTORS = [
    '.card',
    '.card-header',
    '.card-body',
    '.sidebar',
    '.sidebar-brand',
    '.nav-link',
    '.nav-link.active',
    '.nav-section',
    '.stat-val',
    '.stat-lbl',
    '.bar',
    '.bar-fill',
    '.btn',
    '.tab',
    '.tab.active',
    '.tbl th',
    '.field-input',
    '.page-header',
    'body',
  ];

  function getSheetsForTheme(themeName) {
    // Find the <link> element that loaded the theme CSS
    return Array.from(document.styleSheets).filter(sheet => {
      try {
        return sheet.href && sheet.href.includes('/themes/' + themeName);
      } catch(e) {
        return false;
      }
    });
  }

  function getSelectorsInSheet(sheet) {
    const selectors = new Set();
    try {
      const rules = Array.from(sheet.cssRules || []);
      rules.forEach(rule => {
        if (rule.selectorText) {
          // Split compound selectors: ".card, .card-body" → [".card", ".card-body"]
          rule.selectorText.split(',').forEach(s => selectors.add(s.trim()));
        }
      });
    } catch(e) {
      console.warn('[ThemeValidator] Could not read CSS rules (CORS?):', e.message);
    }
    return selectors;
  }

  function validate(themeName) {
    if (!themeName || themeName === 'default') {
      console.log('[ThemeValidator] Default theme — no validation needed');
      return { ok: true, missing: [], covered: [] };
    }

    const sheets = getSheetsForTheme(themeName);
    if (sheets.length === 0) {
      console.error(`[ThemeValidator] Theme CSS not loaded: themes/${themeName}.css`);
      return { ok: false, missing: REQUIRED_SELECTORS, covered: [] };
    }

    const covered = new Set();
    sheets.forEach(sheet => {
      getSelectorsInSheet(sheet).forEach(sel => covered.add(sel));
    });

    const missing = REQUIRED_SELECTORS.filter(req => {
      // Check if any covered selector matches this requirement
      // Allow partial match: ".card" covers ".card-red" etc.
      return !Array.from(covered).some(c =>
        c === req || c.startsWith(req + ' ') || c.startsWith(req + ':') ||
        c.startsWith(req + '.') || c.endsWith(' ' + req)
      );
    });

    if (missing.length > 0) {
      console.group(`%c[ThemeValidator] Theme "${themeName}" is INCOMPLETE`, 'color: orange; font-weight: bold');
      console.warn(`Missing ${missing.length} required selector(s):`);
      missing.forEach(m => console.warn('  ✗', m));
      console.warn('Add these selectors to:', `web/static/themes/${themeName}.css`);
      console.groupEnd();
      return { ok: false, missing, covered: Array.from(covered) };
    }

    console.log(`%c[ThemeValidator] Theme "${themeName}" ✓ All ${REQUIRED_SELECTORS.length} selectors covered`, 'color: #4ade80; font-weight: bold');
    return { ok: true, missing: [], covered: Array.from(covered) };
  }

  // Auto-run in debug mode
  if (window.BYTESLUT_DEBUG) {
    document.addEventListener('DOMContentLoaded', () => {
      const themeLink = document.querySelector('link[href*="/themes/"]');
      if (themeLink) {
        const match = themeLink.href.match(/\/themes\/([^.]+)\.css/);
        if (match) setTimeout(() => validate(match[1]), 500);
      }
    });
  }

  return { validate, REQUIRED_SELECTORS };
})();
