"""Download only the configured, digest-pinned public tokenizer files."""

import argparse
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml
from tokenizers import Tokenizer

from enterprise_mind.infrastructure.model_transport import model_proxy_url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/tokenizers.yaml"))
    args = parser.parse_args()
    try:
        config = args.config.resolve()
        entries = yaml.safe_load(config.read_text(encoding="utf-8"))["tokenizers"]
        for role, entry in entries.items():
            target = (config.parent / entry["file"]).resolve()
            if (
                target.is_file()
                and hashlib.sha256(target.read_bytes()).hexdigest() == entry["sha256"]
            ):
                print(f"{role}: already verified")
                continue
            url = urlsplit(entry["url"])
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("invalid download endpoint")
            with httpx.Client(
                timeout=60, trust_env=False, proxy=model_proxy_url(), follow_redirects=True
            ) as client:
                with client.stream("GET", entry["url"]) as response:
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 40 * 1024 * 1024:
                            raise ValueError("tokenizer too large")
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError("digest mismatch")
            Tokenizer.from_str(content.decode("utf-8"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            print(f"{role}: downloaded and verified")
        return 0
    except Exception:
        print("Tokenizer preparation failed; existing provider configuration was not changed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
