---
name: css-best-practices-agent
description: CSS best practices agent for writing maintainable, scalable, and performant stylesheets. Use when writing CSS, styling components, or reviewing stylesheet architecture. Covers modern CSS features (2024-2025), naming conventions, organization, and common pitfalls to avoid.
---

# CSS Best Practices

## Core Principles

1. **Consistency > Cleverness** - Predictable code is maintainable code
2. **Specificity is debt** - Keep selectors as flat as possible
3. **Components are independent** - Styles shouldn't leak or depend on context
4. **Progressive enhancement** - Start with basics, layer on enhancements

---

## Modern CSS Features to USE (2024-2025)

### Container Queries (Preferred over media queries for components)
```css
/* Define a container */
.card-wrapper {
  container-type: inline-size;
  container-name: card;
}

/* Style based on container, not viewport */
@container card (min-width: 400px) {
  .card {
    display: flex;
    flex-direction: row;
  }
}
```

### CSS Nesting (Native, no preprocessor needed)
```css
.card {
  padding: 1rem;
  
  & .title {
    font-size: 1.25rem;
  }
  
  &:hover {
    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
  }
  
  @media (min-width: 768px) {
    padding: 2rem;
  }
}
```

### Cascade Layers (Control specificity wars)
```css
/* Define layer order - later layers win */
@layer reset, base, components, utilities;

@layer reset {
  * { margin: 0; box-sizing: border-box; }
}

@layer components {
  .btn { /* component styles */ }
}

@layer utilities {
  .mt-4 { margin-top: 1rem !important; }  /* Utilities always win */
}
```

### :has() Selector (Parent selector!)
```css
/* Style parent based on child */
.form-group:has(input:invalid) {
  border-color: red;
}

/* Style sibling based on state */
label:has(+ input:focus) {
  color: blue;
}

/* Card with image vs without */
.card:has(img) {
  padding: 0;
}
```

### text-wrap: balance (For headings)
```css
h1, h2, h3 {
  text-wrap: balance;  /* Balances line lengths automatically */
}
```

### Logical Properties (Better internationalization)
```css
/* PREFER: Works for LTR and RTL */
.element {
  margin-inline-start: 1rem;  /* Left in LTR, right in RTL */
  padding-block: 1rem;        /* Top and bottom */
}

/* AVOID: Fixed direction */
.element {
  margin-left: 1rem;
}
```

### Color Functions
```css
:root {
  --brand: oklch(65% 0.15 250);
}

.element {
  /* Automatic light/dark variants */
  background: oklch(from var(--brand) calc(l + 20%) c h);
  border-color: oklch(from var(--brand) calc(l - 10%) c h);
}
```

---

## CSS Custom Properties (Variables)

### Define at :root for globals
```css
:root {
  /* Spacing scale */
  --space-xs: 0.25rem;
  --space-sm: 0.5rem;
  --space-md: 1rem;
  --space-lg: 1.5rem;
  --space-xl: 2rem;
  
  /* Colors - semantic naming */
  --color-primary: #3b82f6;
  --color-primary-hover: #2563eb;
  --color-danger: #ef4444;
  --color-success: #22c55e;
  --color-text: #1f2937;
  --color-text-muted: #6b7280;
  --color-border: #e5e7eb;
  --color-surface: #ffffff;
  
  /* Typography */
  --font-sans: system-ui, -apple-system, sans-serif;
  --font-mono: ui-monospace, 'Cascadia Code', monospace;
  --text-sm: 0.875rem;
  --text-base: 1rem;
  --text-lg: 1.125rem;
  --text-xl: 1.25rem;
  
  /* Borders & Shadows */
  --radius-sm: 0.25rem;
  --radius-md: 0.5rem;
  --radius-lg: 1rem;
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.05);
  --shadow-md: 0 4px 6px rgba(0,0,0,0.1);
  
  /* Transitions */
  --transition-fast: 150ms ease;
  --transition-base: 200ms ease;
}
```

### Component-scoped variables
```css
.btn {
  --btn-padding-x: var(--space-md);
  --btn-padding-y: var(--space-sm);
  --btn-bg: var(--color-primary);
  --btn-color: white;
  
  padding: var(--btn-padding-y) var(--btn-padding-x);
  background: var(--btn-bg);
  color: var(--btn-color);
}

.btn--large {
  --btn-padding-x: var(--space-lg);
  --btn-padding-y: var(--space-md);
}
```

---

## Naming Convention: BEM (Block Element Modifier)

```css
/* Block: Standalone component */
.card { }

/* Element: Part of a block (double underscore) */
.card__header { }
.card__body { }
.card__footer { }

/* Modifier: Variation of block/element (double hyphen) */
.card--featured { }
.card__header--compact { }
```

### BEM Guidelines
- Blocks are independent, reusable components
- Elements belong to blocks, can't exist outside them
- Modifiers change appearance/behavior, not structure
- Never nest BEM selectors more than one level: `.card__header` ✓ / `.card__header__title` ✗
- For deeper nesting, create a new block or flatten: `.card__title` ✓

---

## File Organization (ITCSS-inspired)

```
styles/
├── 1-settings/       # Variables, configs (no CSS output)
│   ├── _colors.css
│   ├── _typography.css
│   └── _spacing.css
├── 2-tools/          # Mixins, functions (no CSS output)
├── 3-generic/        # Reset, normalize, box-sizing
│   └── _reset.css
├── 4-elements/       # Bare HTML elements (h1, a, input)
│   └── _base.css
├── 5-objects/        # Layout patterns (grid, container)
│   ├── _container.css
│   └── _grid.css
├── 6-components/     # UI components (cards, buttons, forms)
│   ├── _button.css
│   ├── _card.css
│   └── _form.css
├── 7-utilities/      # Helper classes (.mt-4, .text-center)
│   └── _utilities.css
└── main.css          # Import all in order
```

### main.css import order
```css
/* Settings & Tools first (they don't output CSS) */
@import '1-settings/_colors.css';
@import '1-settings/_typography.css';

/* Generic & Elements */
@import '3-generic/_reset.css';
@import '4-elements/_base.css';

/* Objects, Components, Utilities (increasing specificity) */
@import '5-objects/_container.css';
@import '6-components/_button.css';
@import '7-utilities/_utilities.css';
```

---

## Specificity Rules

### Keep it LOW
```css
/* GOOD: Single class (specificity 0-1-0) */
.btn { }
.card { }
.nav-item { }

/* AVOID: Unnecessary nesting (higher specificity) */
div.container .card .btn { }  /* specificity 0-3-1 */

/* NEVER: IDs for styling (0-1-0-0, hard to override) */
#submit-btn { }
```

### Specificity Hierarchy (lowest to highest)
1. Type selectors: `div`, `p`, `a`
2. Class selectors: `.btn`, `.card`
3. Attribute selectors: `[type="text"]`
4. ID selectors: `#header`
5. Inline styles: `style=""`
6. `!important` (avoid except utilities)

### When specificity is needed
```css
/* Use :where() to keep specificity at 0 */
:where(.card, .panel, .box) {
  border-radius: var(--radius-md);
}

/* Use :is() when you want the highest specificity of the list */
:is(h1, h2, h3) {
  line-height: 1.2;
}
```

---

## Layout Patterns

### Flexbox: One dimension (row OR column)
```css
/* Horizontal navigation */
.nav {
  display: flex;
  gap: var(--space-md);
  align-items: center;
}

/* Vertical stack */
.stack {
  display: flex;
  flex-direction: column;
  gap: var(--space-md);
}

/* Space between with centering */
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
```

### Grid: Two dimensions (rows AND columns)
```css
/* Auto-fit card grid */
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--space-lg);
}

/* Named areas for layouts */
.app-layout {
  display: grid;
  grid-template-areas:
    "header header"
    "sidebar main"
    "footer footer";
  grid-template-columns: 250px 1fr;
  grid-template-rows: auto 1fr auto;
  min-height: 100vh;
}

.header { grid-area: header; }
.sidebar { grid-area: sidebar; }
.main { grid-area: main; }
.footer { grid-area: footer; }
```

### Container utility
```css
.container {
  width: min(100% - 2rem, 1200px);
  margin-inline: auto;
}
```

---

## Common Mistakes to AVOID

### ❌ DON'T: Over-qualify selectors
```css
/* BAD */
div.card { }
button.btn.btn--primary { }

/* GOOD */
.card { }
.btn--primary { }
```

### ❌ DON'T: Use magic numbers
```css
/* BAD */
.element {
  margin-top: 37px;
  padding: 13px;
}

/* GOOD */
.element {
  margin-top: var(--space-lg);
  padding: var(--space-md);
}
```

### ❌ DON'T: Rely on element order
```css
/* BAD: Breaks if HTML order changes */
.card > div:first-child { }
.card > div:nth-child(3) { }

/* GOOD: Explicit classes */
.card__header { }
.card__actions { }
```

### ❌ DON'T: Mix layout and cosmetics
```css
/* BAD: One selector doing too much */
.card {
  display: flex;
  flex-direction: column;
  background: white;
  border-radius: 8px;
  box-shadow: 0 2px 4px rgba(0,0,0,0.1);
  padding: 16px;
  margin-bottom: 24px;
}

/* GOOD: Separation of concerns */
.card {
  background: var(--color-surface);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-sm);
  padding: var(--space-md);
}
/* Layout handled by parent or utility */
```

### ❌ DON'T: Use negative z-index
```css
/* BAD: Creates layering chaos */
.element { z-index: -1; }

/* GOOD: Use stacking context intentionally */
.element { position: relative; z-index: 1; }
```

### ❌ DON'T: Animate expensive properties
```css
/* BAD: Triggers layout/paint */
.element {
  transition: width 0.3s, height 0.3s, top 0.3s, left 0.3s;
}

/* GOOD: Only animate composite properties */
.element {
  transition: transform 0.3s, opacity 0.3s;
}
```

---

## Responsive Design

### Mobile-first breakpoints
```css
/* Base: Mobile */
.element {
  flex-direction: column;
}

/* Tablet and up */
@media (min-width: 768px) {
  .element {
    flex-direction: row;
  }
}

/* Desktop and up */
@media (min-width: 1024px) {
  .element {
    max-width: 1200px;
  }
}
```

### Prefer fluid over fixed
```css
/* GOOD: Fluid with constraints */
.container {
  width: clamp(320px, 90vw, 1200px);
  padding: clamp(1rem, 3vw, 2rem);
}

/* Font scaling */
h1 {
  font-size: clamp(1.5rem, 4vw, 3rem);
}
```

---

## Performance

### Critical CSS
- Inline critical above-fold styles in `<head>`
- Defer non-critical CSS with `media="print"` trick

### Reduce Repaints
```css
/* Use will-change sparingly, only when needed */
.animated-element {
  will-change: transform;
}

/* Remove when animation completes (via JS) */
```

### Efficient Selectors
```css
/* FAST: Single class */
.btn { }

/* SLOWER: Descendant with type */
.container div { }

/* SLOWEST: Universal with descendant */
* .content { }
```

### Reduce Unused CSS
- Remove dead code regularly
- Use PurgeCSS or similar in production builds
- Split CSS by route if possible

---

## Accessibility in CSS

```css
/* Focus visible for keyboard users */
:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

/* Remove focus for mouse users */
:focus:not(:focus-visible) {
  outline: none;
}

/* Respect motion preferences */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}

/* Hide visually but keep for screen readers */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

/* Ensure sufficient color contrast: 4.5:1 for text, 3:1 for large text */
```

---

## Quick Reference: Property Order

Group properties by type for consistency:

```css
.element {
  /* 1. Positioning */
  position: relative;
  top: 0;
  right: 0;
  z-index: 1;
  
  /* 2. Display & Box Model */
  display: flex;
  flex-direction: column;
  width: 100%;
  padding: var(--space-md);
  margin: 0;
  
  /* 3. Typography */
  font-family: var(--font-sans);
  font-size: var(--text-base);
  line-height: 1.5;
  color: var(--color-text);
  
  /* 4. Visual */
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-sm);
  
  /* 5. Misc */
  cursor: pointer;
  transition: var(--transition-base);
}
```

---

## Code Review Checklist

```
□ Variables used for all colors, spacing, typography
□ No magic numbers or hard-coded values
□ Selectors use single class (no IDs, minimal nesting)
□ BEM naming convention followed
□ No duplicate property declarations
□ Responsive behavior uses container queries where possible
□ Animations use transform/opacity only
□ Focus states defined for all interactive elements
□ Reduced motion media query respected
□ No !important except in utility classes
□ Consistent property order
□ Logical properties used for directional values
```
