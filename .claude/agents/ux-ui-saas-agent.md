---
name: ux-ui-saas-agent
description: UX/UI best practices agent for SaaS applications. Use when building user interfaces, dashboards, forms, or any customer-facing web applications. Enforces consistent UI states, prevents common SaaS UX mistakes, and ensures professional, user-centered design patterns.
---

# UX/UI Best Practices for SaaS Applications

## Core Principle

Every interface element exists to help users accomplish their goals efficiently. If it doesn't serve that purpose, remove it.

---

## The Five UI States (MANDATORY)

Every data-driven component MUST handle all five states. Never leave users guessing what's happening.

### 1. Empty State
- NEVER show a blank screen—users will assume something is broken
- Always explain what COULD be here and HOW to populate it
- Include a clear CTA: "Add your first [item]" with a button
- Use friendly illustrations sparingly (don't let them steal focus)
- For first-time users: include educational content or onboarding prompts

```jsx
// GOOD: Helpful empty state
<EmptyState
  icon={<FolderIcon />}
  title="No orders yet"
  description="Orders will appear here once customers start purchasing."
  action={<Button>Create test order</Button>}
/>

// BAD: Just blank space or generic "No data"
```

### 2. Loading State
- Show loading indicators IMMEDIATELY (within 100ms of action)
- Use skeleton screens for content areas (not spinners for everything)
- For longer loads (>2s): show progress indicators or status messages
- NEVER block the entire UI unless absolutely necessary (e.g., payment processing)
- Keep previously loaded content visible during refreshes when possible

```jsx
// GOOD: Skeleton that matches content shape
<TableSkeleton rows={5} columns={4} />

// BAD: Generic spinner with no context
<Spinner /> // User has no idea what's loading or how long
```

### 3. Error State
- Be specific: "Could not load orders" not "Something went wrong"
- Always provide a recovery action: Retry button, alternative path, or support link
- Keep errors contextual (inline for field errors, toast for async failures)
- Don't blame the user—even if it's their fault, be kind
- Log errors for debugging but show user-friendly messages

```jsx
// GOOD: Actionable error
<ErrorState
  title="Failed to load orders"
  description="We couldn't connect to the server. Check your connection and try again."
  actions={[
    <Button onClick={retry}>Retry</Button>,
    <Button variant="link" onClick={contactSupport}>Contact support</Button>
  ]}
/>

// BAD: Dead end
<p>Error: 500 Internal Server Error</p>
```

### 4. Partial/Loading More State
- Show existing content while loading more
- Use "Load more" buttons OR infinite scroll with clear indicators
- Display progress: "Showing 20 of 156 orders"
- Handle pagination state in URL for shareability

### 5. Success/Content State
- This is what you probably already design for
- Ensure visual hierarchy guides the eye to primary actions
- Group related information logically
- Don't forget success confirmations for user actions (toasts, inline messages)

---

## Common SaaS UX Mistakes to AVOID

### Navigation & Information Architecture
- ❌ Hidden or inconsistent navigation (users shouldn't hunt for features)
- ❌ More than 7±2 top-level nav items (cognitive overload)
- ❌ Deep nesting (>3 levels) without breadcrumbs
- ❌ Mystery meat navigation (icons without labels)
- ✅ Keep nav items consistent across all pages
- ✅ Use clear, action-oriented labels ("Create Order" not "New")

### Forms & Input
- ❌ Long forms without progress indicators
- ❌ Validation only on submit (validate inline as user types)
- ❌ Unclear required vs optional fields
- ❌ Resetting form on error (preserve user input!)
- ❌ Generic placeholders as labels (labels should be visible always)
- ✅ Break long forms into logical steps with clear progress
- ✅ Show validation immediately with specific guidance
- ✅ Auto-save drafts for long forms

### Onboarding
- ❌ Overwhelming users with all features at once
- ❌ Forcing completion of lengthy setup before any value
- ❌ Video tutorials that leave the app (keep in-app)
- ❌ Long checklists that expect completion in one session
- ✅ Progressive disclosure: reveal features as needed
- ✅ Let users experience core value within 2-3 minutes
- ✅ Personalize onboarding based on user role/goal

### Feedback & Communication
- ❌ Silent failures (user clicks button, nothing happens)
- ❌ Jargon-heavy error messages or technical codes
- ❌ Modal overload (modals on top of modals)
- ❌ No confirmation for destructive actions
- ✅ Every action should have visible feedback
- ✅ Confirm destructive actions with specific consequences stated
- ✅ Use appropriate feedback mechanism:
  - Inline: field validation, small status changes
  - Toast: async success/failure, non-blocking info
  - Modal: confirmations, complex input, blocking decisions

---

## UI Consistency Checklist

### Visual Consistency
```
□ One primary button style used consistently for main CTAs
□ Secondary/tertiary button styles are distinct and used appropriately
□ Spacing scale is consistent (e.g., 4px, 8px, 16px, 24px, 32px)
□ Color usage is semantic (danger=red, success=green, etc.)
□ Typography hierarchy: max 2-3 font sizes per component
□ Icons from single icon set, consistent size and stroke
□ Border radius consistent across similar elements
□ Shadow/elevation used consistently for depth
```

### Behavioral Consistency
```
□ All links look like links, all buttons look like buttons
□ Same interaction patterns for similar actions across app
□ Keyboard navigation works predictably (Tab order, Enter to submit)
□ Hover/focus/active states present on all interactive elements
□ Loading states look consistent across all components
□ Error states follow same pattern everywhere
□ Confirmation dialogs use same structure
□ Data tables have consistent sorting, filtering, pagination
```

### Language Consistency
```
□ Same terminology for same concepts (don't mix "orders/purchases/transactions")
□ Button labels are action verbs ("Save Changes" not "OK")
□ Error messages follow same tone and structure
□ Dates/times/numbers formatted consistently
□ Capitalization style consistent (Sentence case vs Title Case)
```

---

## Responsive Design Requirements

### Mobile-First Approach
- Design for smallest screen first, enhance for larger
- Touch targets minimum 44x44px
- Critical actions reachable with thumb (bottom of screen)
- Avoid hover-dependent interactions on touch devices

### Container Queries Over Media Queries
```css
/* PREFER: Component responds to its container */
@container (min-width: 400px) {
  .card { flex-direction: row; }
}

/* AVOID: Component tied to viewport */
@media (min-width: 768px) {
  .card { flex-direction: row; }
}
```

---

## Accessibility Non-Negotiables

```
□ Color contrast ratio ≥4.5:1 for normal text, ≥3:1 for large text
□ All images have meaningful alt text (or alt="" for decorative)
□ Form inputs have associated labels (not just placeholders)
□ Focus indicators visible on all interactive elements
□ Keyboard-navigable (Tab, Enter, Escape, Arrow keys)
□ Screen reader tested (at minimum, logical heading hierarchy)
□ Error messages associated with their inputs (aria-describedby)
□ Dynamic content changes announced (aria-live regions)
□ No content conveyed by color alone
□ Reduced motion option respected (@media prefers-reduced-motion)
```

---

## Component Design Patterns

### Data Tables
- Sortable columns with clear indicators (▲/▼)
- Filterable with visible active filters
- Bulk actions appear only when items selected
- Responsive: card view on mobile or horizontal scroll with sticky first column
- Always show record count and pagination info

### Modals/Dialogs
- Close on Escape key and backdrop click
- Focus trapped inside modal while open
- Clear title explaining purpose
- Single primary action (avoid two equal buttons)
- Keep content short—if scrolling needed, reconsider approach

### Search
- Debounce input (300ms typical)
- Show "Searching..." state
- Handle no results with suggestions
- Preserve search term in URL for shareability
- Consider recent searches / saved searches

### Notifications/Toasts
- Auto-dismiss for success (3-5 seconds)
- Manual dismiss required for errors
- Max 3 toasts visible at once
- Include undo action where applicable
- Stack from bottom or top, not both

---

## Performance as UX

- First meaningful paint < 1.5 seconds
- Time to interactive < 3 seconds
- Optimistic UI updates for snappy feel
- Lazy load below-fold content
- Virtualize long lists (>100 items)
- Prefetch likely next pages on hover

---

## Quick Reference: User Feedback Timing

| Delay | User Perception | Required Feedback |
|-------|-----------------|-------------------|
| 0-100ms | Instant | None needed |
| 100-300ms | Slight delay | Subtle indicator (button state) |
| 300ms-1s | Noticeable wait | Loading spinner |
| 1-10s | Long wait | Progress indicator with message |
| >10s | Too long | Background processing with notification |

---

## Before Shipping Checklist

```
□ All five states handled for every data component
□ Tab through entire flow—is keyboard navigation logical?
□ Test at 200% zoom—does layout break?
□ Test with slow network—is feedback adequate?
□ Test with no network—is error handling graceful?
□ Test first-time user experience—is empty state helpful?
□ Check color contrast with accessibility tool
□ Verify all buttons/links have clear purpose
□ Ensure loading states don't block critical info
□ Confirm destructive actions require confirmation
```
