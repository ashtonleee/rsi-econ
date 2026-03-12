import argparse
import json

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()

    response = httpx.get(args.url, timeout=5.0)
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
