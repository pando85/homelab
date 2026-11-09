#!/usr/bin/env python3
"""Regression test for the Grafana JWT role_attribute_path JMESPath expression.

Verifies that the role mapping expression in system/monitoring/values.yaml
uses exact whitespace-delimited token matching on the `scope` STRING field
(space-separated tokens from OAuth2 access tokens), not substring matching.

The expression must:
  - Map 'admin' scope token -> 'Admin' role
  - Map 'editor' scope token -> 'Editor' role
  - Default to 'Viewer' for any other scope set
  - NOT match substrings (e.g., 'administrator' must not match 'admin')
  - Handle missing/null scope gracefully -> Viewer
  - Give admin precedence over editor when both present

This test uses the actual jmespath library to evaluate the expression against
various scope strings, including adversarial cases.

    python3 test/mcp-gateway/grafana-role-expression-test.py

Requires: python3 with jmespath (pip install jmespath).
"""
import sys
import re
from pathlib import Path

try:
    import jmespath
except ImportError:
    print("ERROR: jmespath library not installed. Run: pip install jmespath")
    sys.exit(2)


# The role_attribute_path expression from system/monitoring/values.yaml
# This must match what's in the actual config file.
# Uses boundary checks on the `scope` STRING field (space-separated tokens).
# Uses `scope || ''` to handle missing/null scope gracefully.
ROLE_EXPRESSION = (
    "((starts_with(scope || '', 'admin ') || contains(scope || '', ' admin ') || "
    "ends_with(scope || '', ' admin') || (scope || '') == 'admin') && 'Admin') || "
    "((starts_with(scope || '', 'editor ') || contains(scope || '', ' editor ') || "
    "ends_with(scope || '', ' editor') || (scope || '') == 'editor') && 'Editor') || 'Viewer'"
)

# Test cases: (scope_string, expected_role, description)
# scope is a space-separated STRING, as in OAuth2 access tokens
TEST_CASES = [
    # Happy path: exact token matches
    ("admin", "Admin", "single admin token -> Admin"),
    ("editor", "Editor", "single editor token -> Editor"),
    ("openid email profile", "Viewer", "basic scopes without role -> Viewer"),
    ("openid email profile editor", "Editor", "editor at end -> Editor"),
    ("editor openid email profile", "Editor", "editor at start -> Editor"),
    ("openid editor email", "Editor", "editor in middle -> Editor"),
    ("openid email profile admin", "Admin", "admin at end -> Admin"),
    ("admin openid email profile", "Admin", "admin at start -> Admin"),
    ("openid admin email", "Admin", "admin in middle -> Admin"),

    # Priority: admin takes precedence over editor
    ("admin editor", "Admin", "both admin and editor -> Admin (admin wins)"),
    ("editor admin", "Admin", "editor before admin -> Admin (admin wins)"),
    ("openid admin editor email", "Admin", "admin and editor among others -> Admin"),

    # Adversarial: substring matches must NOT trigger
    ("noneditor", "Viewer", "'noneditor' must not match 'editor' (prefix attack)"),
    ("editorial", "Viewer", "'editorial' must not match 'editor' (suffix attack)"),
    ("myeditor", "Viewer", "'myeditor' must not match 'editor' (prefix attack)"),
    ("editors", "Viewer", "'editors' must not match 'editor' (plural suffix)"),
    ("administrator", "Viewer", "'administrator' must not match 'admin' (suffix attack)"),
    ("admin123", "Viewer", "'admin123' must not match 'admin' (suffix attack)"),
    ("superadmin", "Viewer", "'superadmin' must not match 'admin' (prefix attack)"),
    ("admins", "Viewer", "'admins' must not match 'admin' (plural suffix)"),
    ("openid noneditor email", "Viewer", "'noneditor' among others -> Viewer"),
    ("openid administrator email", "Viewer", "'administrator' among others -> Viewer"),

    # Edge cases: empty, missing, or null scope
    ("", "Viewer", "empty string -> Viewer"),
    (None, "Viewer", "null/missing scope -> Viewer"),

    # Case sensitivity (JMESPath is case-sensitive)
    ("Admin", "Viewer", "'Admin' (capitalized) must not match 'admin' (case-sensitive)"),
    ("Editor", "Viewer", "'Editor' (capitalized) must not match 'editor' (case-sensitive)"),
    ("ADMIN", "Viewer", "'ADMIN' (uppercase) must not match 'admin' (case-sensitive)"),
    ("EDITOR", "Viewer", "'EDITOR' (uppercase) must not match 'editor' (case-sensitive)"),
]


def test_expression():
    """Evaluate the role expression against all test cases."""
    compiled = jmespath.compile(ROLE_EXPRESSION)
    passed = 0
    failed = 0
    failures = []

    print(f"Testing role_attribute_path expression:")
    print(f"  {ROLE_EXPRESSION}")
    print()

    for scope, expected, description in TEST_CASES:
        # Build a JWT-like claims object with the scope string
        claims = {"scope": scope} if scope is not None else {}
        try:
            result = compiled.search(claims)
        except Exception as e:
            result = f"ERROR: {e}"

        if result == expected:
            passed += 1
            print(f"  PASS  {description}")
            print(f"        scope={scope!r} -> {result}")
        else:
            failed += 1
            failures.append((description, scope, expected, result))
            print(f"  FAIL  {description}")
            print(f"        scope={scope!r} -> expected {expected}, got {result}")

    print()
    print(f"Results: {passed} passed, {failed} failed out of {len(TEST_CASES)} tests")

    if failures:
        print()
        print("Failed tests:")
        for desc, scope, expected, got in failures:
            print(f"  - {desc}")
            print(f"    scope={scope!r}, expected={expected}, got={got}")
        return 1

    return 0


def test_config_file_consistency():
    """Verify the expression in the test matches the config file."""
    values_file = Path(__file__).parent.parent.parent / "system" / "monitoring" / "values.yaml"
    if not values_file.exists():
        print(f"WARNING: Could not find {values_file} to verify expression consistency")
        return 0

    content = values_file.read_text()

    # Extract the role_attribute_path from the auth.jwt section
    # Look for the line after "auth.jwt:" that contains role_attribute_path
    jwt_section = False
    for line in content.splitlines():
        if "auth.jwt:" in line:
            jwt_section = True
        elif jwt_section and line.strip().startswith("role_attribute_path:"):
            # Extract the expression: remove the key, then strip surrounding quotes
            # The line format is: role_attribute_path: "expression"
            match = re.search(r'role_attribute_path:\s*(.+)$', line)
            if match:
                file_expression = match.group(1).strip()
                # Remove surrounding double quotes if present
                if file_expression.startswith('"') and file_expression.endswith('"'):
                    file_expression = file_expression[1:-1]
                elif file_expression.startswith("'") and file_expression.endswith("'"):
                    file_expression = file_expression[1:-1]

                if file_expression == ROLE_EXPRESSION:
                    print(f"PASS  Expression in config file matches test")
                    return 0
                else:
                    print(f"FAIL  Expression mismatch!")
                    print(f"  Config file: {file_expression}")
                    print(f"  Test expects: {ROLE_EXPRESSION}")
                    return 1
        elif jwt_section and line.strip() and not line.startswith(" ") and not line.startswith("\t"):
            # We've left the auth.jwt section
            break

    print(f"WARNING: Could not find role_attribute_path in auth.jwt section of {values_file}")
    return 0


def main():
    print("=" * 70)
    print("Grafana JWT role_attribute_path regression test")
    print("=" * 70)
    print()

    # Test 1: Verify config file consistency
    print("--- Config file consistency check ---")
    config_ok = test_config_file_consistency()
    print()

    # Test 2: Evaluate the expression
    print("--- Expression evaluation tests ---")
    expr_ok = test_expression()
    print()

    if config_ok == 0 and expr_ok == 0:
        print("=" * 70)
        print("All tests passed!")
        print("=" * 70)
        return 0
    else:
        print("=" * 70)
        print("Some tests failed!")
        print("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
