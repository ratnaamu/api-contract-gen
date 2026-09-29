"""python -m demo_app [--v2] [--port 8001] [--log live_logs.jsonl] [--error-rate 0.02] [--seed N]"""
import argparse
import os

import uvicorn

from .app import DEFAULT_ERROR_RATE, DEFAULT_LOG_PATH, create_app, is_v2_from_env


def main() -> None:
    ap = argparse.ArgumentParser(description="Passport application demo service (writes JSON-line logs)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--v2", action="store_true",
                    help="v2 contract: date_of_birth -> birth_date, new required emergency_contact "
                         "(same as PASSPORT_API_VERSION=2)")
    ap.add_argument("--log", default=os.environ.get("PASSPORT_APP_LOG", DEFAULT_LOG_PATH),
                    help="JSON-lines log file the contract generator watches")
    ap.add_argument("--error-rate", type=float,
                    default=float(os.environ.get("PASSPORT_ERROR_RATE", DEFAULT_ERROR_RATE)),
                    help="chance of a random 500 per API call")
    ap.add_argument("--seed", type=int, default=None, help="seed for the random 500s (reproducible runs)")
    a = ap.parse_args()

    v2 = a.v2 or is_v2_from_env()
    app = create_app(v2=v2, log_path=a.log, error_rate=a.error_rate, seed=a.seed)
    print(f"passport service v{2 if v2 else 1}: http://{a.host}:{a.port}   (form at /, logging API calls to {a.log})",
          flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
