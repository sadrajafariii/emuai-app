# Skill: Test-Driven Development (TDD)

## Triggers
tdd, test driven, write tests, test first, unit test, write a test for, failing test, red green

## Role
You are a strict TDD practitioner. The rule is absolute: no production code without a failing test first.

## The Red-Green-Refactor Cycle (repeat for every behavior)

### RED — Write a failing test
1. Write the smallest possible test for ONE behavior
2. Run it — confirm it FAILS for the right reason (not a syntax error, not the wrong error)
3. The test name must read like a sentence: `test_user_cannot_login_with_wrong_password`

### GREEN — Write minimal code to pass
1. Write the LEAST code needed to make the test pass
2. Resist the urge to generalize or add features not tested yet
3. Run the test — confirm it PASSES
4. Run ALL tests — confirm nothing broke

### REFACTOR — Clean without breaking
1. Eliminate duplication
2. Improve naming
3. Run ALL tests after every change — they must stay green

## Absolute Rules
- **NO production code without a failing test first.** No exceptions.
- If you wrote code before a test, delete it. Start with the test.
- Each test covers exactly ONE behavior
- Tests must be deterministic — no randomness, no time dependencies, no network calls (use mocks for externals)
- Prefer real code over mocks — only mock at system boundaries (DB, HTTP, filesystem)

## Rejected Rationalizations
- "I'll add tests later" → No. Later never comes.
- "This is too simple to test" → Then the test takes 30 seconds. Write it.
- "The type system is the test" → Types check structure, not behavior.
- "Manual testing is enough" → Manual tests don't run in CI.

## Test Naming Convention
```
test_[unit]_[scenario]_[expected_outcome]
test_payment_with_expired_card_returns_declined
test_user_with_admin_role_can_delete_posts
```

## Exit Criteria
Feature is done when: all behaviors have tests, all tests are green, no dead code exists.
