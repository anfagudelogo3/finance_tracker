"""AWS Lambda entry point.

scripts/deploy.py copies this file to the root of lambda.zip, so the function's
handler setting stays `handler.handler`. All code lives in the finance_tracker
package, which deploy.py installs into the zip alongside the dependencies.
"""

from finance_tracker.handler import handler  # noqa: F401
