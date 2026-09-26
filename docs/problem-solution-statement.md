# ContractGuard — Problem & Solution Statement

When a backend API changes (a field is renamed, a type changes, a required field is added, or an endpoint is removed), the frontend breaks silently. In most REST setups there is no compile-time warning; the failure only surfaces when a user hits a runtime error, or worse, when it is already live in production.

That is the bug ContractGuard exists to kill.

Today, fixing it means a developer stops building and starts doing archaeology: diffing two API versions by eye, grepping the frontend for every usage, hoping they caught them all. It is slow, error-prone, and hits junior developers and fast-moving teams hardest, especially those without a senior engineer who remembers every API change by heart. This is exactly the release, deployment, and maintenance pain this challenge calls out, and in small teams, it happens every sprint.

## The Solution

ContractGuard closes the loop that every existing tool leaves open. OpenAPI linters, Pact contract tests, and `tsc` errors all stop at the same place: raise a flag, hand it to a human, and walk away. ContractGuard does not walk away. It detects the break, explains it in plain English, patches every affected line of frontend code, verifies the fix actually compiles and passes tests, and writes up what it did, fully automated, end to end.

## Under the Hood

Four agents do the work a human used to:

* **Diff Agent:** Compares the backend's OpenAPI spec before and after a change and flags exactly what broke.


* **Impact Agent:** Hunts down every frontend file touching the changed field or endpoint, so no call site gets missed.


* **Repair Agent:** Built on IBM Bob 2.0's Agent mode, writes and applies the real code fix.


* **Verify Agent:** Runs the build and test suite to prove the fix works, looping back to repair on failure rather than ever faking a pass.



## Who It's For

Any team shipping a typed frontend against an API that moves fast, particularly the kind of team where "who broke the build" is a recurring Slack message. Open the Impact Report and a PM, a new hire, or a stressed developer at 2 AM gets an instant, readable answer: what changed, why it matters, and what was already fixed, with no diff-reading required.

## Why It's Different

ContractGuard is not a linter you can shrug off. It is the missing last mile between detecting a break and surviving one: narrow enough to demo in under 90 seconds, and backed by a metric no judge can argue with: a breaking API change going from a silent production incident to detected, explained, fixed, and proven, with zero human hands on the frontend.