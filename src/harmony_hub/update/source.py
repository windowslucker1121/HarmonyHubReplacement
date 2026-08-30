"""Where a hub finds a new release, when nobody is at a dev machine to push one.

Pairs with `bundle.py`/`manifest.py`: `harmony-deploy build` (run by a
person, or by CI -- see `.github/workflows/release.yml` on every `v*` tag
push) produces exactly the bundle/manifest pair `installer.install` already
knows how to consume, and attaches both as GitHub release assets. This
module is the other end -- it finds the latest release and downloads its
assets, entirely read-only against GitHub's public REST API.

No FastAPI import, same convention as `installer.py`: this is tested
against a fake `httpx` transport, not a running app. `check.py` is the
caller that adds caching, polling, and the announce-once behaviour the
Live log and Settings screen show; `api.py`'s routes are a thin translation
of that onto HTTP, the same shape as the signed-push routes already are
onto `installer.install`.

Trust model, spelled out because it is deliberately weaker than a signed
push: there is no HMAC here, because there is no second party holding a
shared secret to sign with -- only GitHub's TLS standing behind "this is
really the release this repo published". That is the same risk class as
`POST /api/update/rollback`, which already needs no signature: at worst it
lets a LAN attacker make this device install a release the configured repo
actually published, not arbitrary code. `manifest.content_sha256` is still
checked before a single byte of the download is trusted (`download` below),
and `installer.install` -> `extract.safe_extract` still get the final say
over what is actually inside the tar, exactly as they do for a signed push.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import List, Optional

import httpx
from pydantic import BaseModel

from .manifest import Manifest

logger = logging.getLogger("HUB.update.source")

#: Pinned so a future breaking change on GitHub's end fails loudly (a 4xx)
#: instead of silently changing shape under this code.
API_VERSION = "2022-11-28"

DEFAULT_TIMEOUT = 15.0
DOWNLOAD_TIMEOUT = 300.0


class ReleaseFeedError(RuntimeError):
    """The release feed could not be read, or does not contain an installable release."""


class AvailableRelease(BaseModel):
    """One GitHub release, already resolved down to what `installer.install` needs."""

    tag: str
    published_at: str = ""
    notes: str = ""
    tar_url: str
    tar_bytes: int
    manifest: Manifest

    @property
    def build_id(self) -> str:
        return self.manifest.build_id


def _find_asset(assets: List[dict], *, suffix: str) -> Optional[dict]:
    matches = [a for a in assets if str(a.get("name", "")).endswith(suffix)]
    if len(matches) > 1:
        logger.warning("release has %d assets ending in %s; using the first", len(matches), suffix)
    return matches[0] if matches else None


async def fetch_latest(
    repo: str, *, client: Optional[httpx.AsyncClient] = None, timeout: float = DEFAULT_TIMEOUT
) -> AvailableRelease:
    """The most recent published GitHub release for `repo` (`"owner/name"`).

    Raises `ReleaseFeedError` for anything that leaves nothing installable:
    no releases published yet, a release missing its bundle or manifest
    asset (one not built by `harmony-deploy build`), a manifest that will
    not parse, or no network at all -- `check.py` is what turns that last
    case into "nothing to report" rather than a crash, since a device with
    no WAN is a normal, not broken, state.
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    try:
        response = await client.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION},
        )
        if response.status_code == 404:
            raise ReleaseFeedError(f"{repo} has no published releases yet")
        response.raise_for_status()
        payload = response.json()

        assets = payload.get("assets") or []
        tar_asset = _find_asset(assets, suffix=".tar.gz")
        manifest_asset = _find_asset(assets, suffix=".manifest.json")
        tag = payload.get("tag_name") or "(untagged)"
        if tar_asset is None or manifest_asset is None:
            raise ReleaseFeedError(
                f"release {tag} is missing its bundle or manifest asset -- "
                "was it built by `harmony-deploy build` / the release workflow?"
            )

        manifest_response = await client.get(manifest_asset["browser_download_url"])
        manifest_response.raise_for_status()
        try:
            manifest = Manifest.model_validate_json(manifest_response.text)
        except Exception as err:
            raise ReleaseFeedError(f"release {tag} has an unreadable manifest: {err}") from err

        return AvailableRelease(
            tag=tag,
            published_at=payload.get("published_at") or "",
            notes=(payload.get("body") or "")[:4000],
            tar_url=tar_asset["browser_download_url"],
            tar_bytes=int(tar_asset.get("size") or 0),
            manifest=manifest,
        )
    except httpx.HTTPError as err:
        raise ReleaseFeedError(f"could not reach the release feed for {repo}: {err}") from err
    finally:
        if owns_client:
            await client.aclose()


async def download(
    url: str,
    dest: "Path | str",
    *,
    expected_sha256: str,
    max_bytes: int,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = DOWNLOAD_TIMEOUT,
) -> int:
    """Streams `url` to `dest`, refusing it mid-stream once it is clearly too big.

    Mirrors `api._save_upload`'s shape for the same reason: a release asset
    is no more trusted than an arbitrary upload just because it came from
    GitHub over TLS. `dest` is removed on any failure, including a hash
    mismatch, so a caller never mistakes a partial or wrong download for a
    usable bundle. Returns the byte count written, once the hash has been
    confirmed to match `expected_sha256`.
    """
    dest = Path(dest)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    hasher = hashlib.sha256()
    size = 0
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ReleaseFeedError(f"asset at {url} exceeds the {max_bytes}-byte limit")
                    hasher.update(chunk)
                    f.write(chunk)
    except httpx.HTTPError as err:
        # Same wrapping `fetch_latest` does, and for the same reason: a 404
        # (an asset a check found a moment ago, now gone -- a release
        # deleted mid-install) or a dropped connection must read as clearly
        # as any other release-feed failure, not surface as an unhandled
        # `HTTPStatusError` two layers away from anyone who could explain it.
        dest.unlink(missing_ok=True)
        raise ReleaseFeedError(f"could not download {url}: {err}") from err
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        if owns_client:
            await client.aclose()

    digest = hasher.hexdigest()
    if digest != expected_sha256:
        dest.unlink(missing_ok=True)
        raise ReleaseFeedError(f"downloaded asset at {url} does not match the manifest's content hash")
    return size


def is_newer(candidate_build_id: str, current_build_id: Optional[str]) -> bool:
    """Whether `candidate_build_id` is a later build than `current_build_id`.

    Plain string comparison is enough: `bundle.make_build_id` always
    prefixes a fixed-width UTC timestamp (`%Y%m%dT%H%M%S`), so lexicographic
    and chronological order agree for as long as that format holds.
    `current_build_id` of `None` -- nothing installed yet -- always counts
    as older.
    """
    if current_build_id is None:
        return True
    return candidate_build_id > current_build_id
