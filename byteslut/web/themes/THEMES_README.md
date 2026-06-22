# ByteSlut Theme System
========================

## Adding a New Theme

1. Create a folder: `web/themes/yourtheme/`
2. Add `theme.json` — metadata and settings
3. Add `style.css` — your complete CSS overrides

The folder name is the theme key used in settings.json → `"ui_layout": "yourtheme"`.

## theme.json spec

```json
{
  "name": "My Theme",
  "description": "What it looks like",
  "author": "Your name",
  "version": "1.0",
  "accent_default": "#00b4d8",
  "requires": ["card", "sidebar", "nav-link"]
}
```

## style.css

Override any CSS classes from the base design system.
The file is loaded AFTER the base styles, so your rules win.

Required selectors (theme validator checks these):
  .card, .card-header, .card-body
  .sidebar, .sidebar-brand
  .nav-link, .nav-link.active
  .main-content
  .btn, .tab, .tab.active
  .tbl, .bar, .bar-fill
  .stat-val, .stat-lbl
  .field-input, .badge
  ::-webkit-scrollbar

## Example

See `web/themes/_example/` for a fully-documented starter template.

## Validation

The app validates your theme on startup and at /api/validate-themes.
If selectors are missing, the dashboard shows a warning in Settings.
