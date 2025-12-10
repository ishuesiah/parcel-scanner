---
name: pragmatic-programmer
description: Code quality guidelines based on "The Pragmatic Programmer" by Andy Hunt and Dave Thomas. Use this skill when writing, reviewing, refactoring, or debugging code. Applies pragmatic principles including ETC (Easier To Change), broken windows theory, Tell Don't Ask, decoupling, deliberate programming, and responsible craftsmanship. Triggers on code creation, code review, refactoring requests, debugging sessions, architecture decisions, and technical debt discussions.
---

# Pragmatic Programmer Guidelines

Guidelines derived from "The Pragmatic Programmer" (20th Anniversary Edition) by Andy Hunt and Dave Thomas. Apply these principles when writing, reviewing, or refactoring code.

## Core Philosophy

**Code is a garden, not a building.** Software requires constant care, pruning, and adaptation. There is no "finished" state—only healthy or neglected.

**ETC: Easier To Change.** When choosing between approaches, ask: "Which makes future changes easier?" This single value resolves most design debates:
- Favor composition over inheritance → ETC
- Decouple modules → ETC  
- Single responsibility → ETC
- Good naming → ETC

## The Broken Windows Rule

**Never leave broken windows.** A single hack, unclear name, or TODO left unaddressed signals abandonment and invites more decay.

When reviewing or writing code, flag broken windows:
- `// TODO` or `// FIXME` comments older than the current task
- Unclear variable/function names
- Copy-pasted code blocks
- Missing error handling
- Magic numbers without explanation
- Commented-out code blocks
- Functions doing multiple unrelated things

**If you can't fix it now:** Add a tracked issue, not just a comment. Comments rot; tickets get reviewed.

## Tell, Don't Ask

**Bad (asking about internal state):**
```python
# Train wreck: 5 levels of coupling
discount = customer.orders.find(order_id).totals.apply_discount(amount)
```

**Good (telling the object what to do):**
```python
# Object handles its own internals
discount = customer.apply_order_discount(order_id, amount)
```

When you see code that:
1. Gets data from an object
2. Makes a decision based on that data
3. Tells the object what to do

→ Refactor so the object makes its own decision.

## Decoupling Principles

**Minimize coupling between modules.** Each module should know as little as possible about others.

Signs of problematic coupling:
- Changing one file requires changes in many others
- You need to understand the whole system to modify one part
- Tests require elaborate setup of unrelated components
- "Train wreck" chains: `a.b.c.d.method()`

Decoupling strategies:
- Pass dependencies as parameters (dependency injection)
- Use interfaces/protocols instead of concrete types
- Emit events instead of direct calls
- Keep data transformations pure (input → output, no side effects)

## Deliberate Programming

**Never program by coincidence.** If code works but you don't know why, you won't know why it breaks.

Before committing code, verify you can answer:
- Why does this work?
- What assumptions does this rely on?
- Could I explain this to another developer?
- What happens when inputs are unexpected?

**"It doesn't work without it" is not a reason to keep code.** Understand or remove it.

## Prototypes vs Tracer Bullets

**Prototypes:** Throwaway code to explore feasibility. Use when you need to:
- Prove a concept is possible
- Explore unfamiliar APIs or libraries
- Test performance characteristics

Prototypes can skip: error handling, edge cases, documentation, tests. They get deleted.

**Tracer bullets:** Minimal working implementation that stays. Use when you need to:
- Establish end-to-end connectivity
- Get early feedback from stakeholders
- Build a foundation to expand

Tracer bullets include: basic error handling, tests, clean structure. They evolve into production code.

## Debugging Methodology

1. **Adopt the right mindset.** Turn off ego. "It can't happen" is wrong—you're looking at the stack trace.

2. **Make it reproducible.** If you can't reproduce it, you can't verify the fix. Aim for single-command reproduction.

3. **Assume the bug is in your code.** Third-party libraries and compilers are rarely the cause. Start with your code.

4. **Binary search the problem.** Comment out half the code. Does it still fail? Narrow systematically.

5. **Explain it to someone (or something).** Rubber duck debugging works. Articulating the problem often reveals it.

6. **Write a failing test first.** Before fixing, write a test that fails with the bug. Then fix. Now you're protected from regression.

## Estimation Guidance

**Never give single-number estimates.** Use ranges or scenarios:

| Duration | Say |
|----------|-----|
| 1-15 days | "days" |
| 3-6 weeks | "weeks" |  
| 2-6 months | "months" |
| Longer | "I need to break this down further" |

**PERT approach:** For complex tasks, consider:
- Optimistic: Everything goes perfectly
- Most likely: Realistic with normal friction
- Pessimistic: Significant obstacles encountered

**Track your estimates.** Compare estimates to actuals. Identify patterns in your over/under estimation.

## Code Review Checklist

When reviewing code (yours or others'), check for:

**Clarity:**
- [ ] Could a new team member understand this?
- [ ] Are names descriptive and consistent?
- [ ] Is the intent clear without comments explaining "what"?

**ETC (Easier To Change):**
- [ ] Are responsibilities clearly separated?
- [ ] Could you change one part without ripple effects?
- [ ] Are dependencies explicit, not hidden?

**Broken Windows:**
- [ ] Any TODOs or FIXMEs being added without tickets?
- [ ] Any copy-paste that should be abstracted?
- [ ] Any magic values that need constants/config?

**Deliberate Design:**
- [ ] Do you understand why every line is there?
- [ ] Are edge cases handled or explicitly documented as out of scope?
- [ ] Would tests catch regressions?

## Refactoring Triggers

Consider refactoring when you encounter:

- **Duplication:** Same logic in multiple places
- **Orthogonality violation:** Unrelated things changing together
- **Outdated knowledge:** Code reflects old requirements
- **Performance bottleneck:** Measured (not assumed) slow spots
- **Awkward usage:** Code that's hard to use correctly

**Refactoring discipline:**
1. Don't refactor and add features simultaneously
2. Have tests before refactoring
3. Take small steps, commit frequently
4. If it's getting worse, revert and try a different approach

## Engineering Daybook Practice

Recommend keeping notes when working on complex problems:

- What you're trying to accomplish
- Approaches you've tried and why they failed/succeeded
- Variable values during debugging
- Decisions made and their rationale

Benefits: More reliable than memory, forces clear thinking, creates searchable history.

## Responsibility & Craftsmanship

**Take ownership.** "Not my code" is not an excuse. If you see a problem, you own helping fix it.

**Offer solutions, not excuses.** Before explaining why something can't be done, think about what can be done.

**Be proud of your work.** Write code you'd be comfortable signing your name to. Anonymous code invites sloppiness.

**Protect users.** Consider: Would I be comfortable using this software? Would I trust it with my data?
