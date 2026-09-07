"""
AI App Builder Pro - Android APK Build Server
===============================================

A production FastAPI service that accepts an Android project as a ZIP
upload, compiles it with the project's own Gradle Wrapper inside a
container that already has OpenJDK 17 and the Android SDK installed,
and returns the resulting debug APK (or, on failure, the real Gradle
build log).

No mock building, no placeholder APK generation: every build is a real
invocation of `./gradlew assembleDebug`.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

MAX_UPLOAD_BYTES = 100 * 1024 * 1024          # 100 MB upload cap
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024      # 1 GB uncompressed cap (zip-bomb guard)
BUILD_TIMEOUT_SECONDS = 10 * 60               # 10 minute max build time
UPLOAD_CHUNK_SIZE = 1024 * 1024               # 1 MB streaming chunks

ANDROID_SDK_ROOT = os.environ.get("ANDROID_SDK_ROOT", "/opt/android-sdk")
ANDROID_HOME = os.environ.get("ANDROID_HOME", ANDROID_SDK_ROOT)

BUILD_WORKSPACE_ROOT = Path(os.environ.get("BUILD_WORKSPACE_ROOT", "/srv/app/build-workspace"))
BUILD_WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("android-build-server")

app = FastAPI(
    title="AI App Builder Pro - Android APK Build Server",
    description="Compiles uploaded Android project ZIPs into installable APKs using Gradle.",
    version="1.0.0",
)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class BuildError(Exception):
    """Raised when a Gradle build fails. Carries the captured build log."""

    def __init__(self, message: str, log: str = ""):
        super().__init__(message)
        self.message = message
        self.log = log


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _new_workspace() -> Path:
    """Create a fresh, unique temporary directory for a single build."""
    workspace = Path(tempfile.mkdtemp(prefix=f"build-{uuid.uuid4().hex}-", dir=str(BUILD_WORKSPACE_ROOT)))
    return workspace


def _cleanup_workspace(workspace: Path) -> None:
    """Remove a build workspace and everything in it, best-effort."""
    try:
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
            logger.info("Cleaned up workspace %s", workspace)
    except Exception:  # noqa: BLE001 - cleanup must never raise
        logger.exception("Failed to clean up workspace %s", workspace)


async def _save_upload_with_limit(upload: UploadFile, destination: Path, max_bytes: int) -> int:
    """
    Stream an UploadFile to disk in chunks, enforcing a hard size limit.
    Raises HTTPException(413) if the limit is exceeded.
    Returns the total number of bytes written.
    """
    total = 0
    with destination.open("wb") as out_file:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                out_file.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Uploaded project exceeds the {max_bytes // (1024 * 1024)} MB limit.",
                )
            out_file.write(chunk)
    await upload.close()
    return total


def _safe_extract_zip(zip_path: Path, extract_to: Path, max_extracted_bytes: int) -> None:
    """
    Safely extract a ZIP archive, protecting against:
      - Zip Slip / path traversal (entries escaping the target directory
        via '..', absolute paths, or symlink tricks).
      - Zip bombs (excessive total uncompressed size).

    Raises HTTPException on any violation.
    """
    if not zipfile.is_zipfile(zip_path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is not a valid ZIP archive.",
        )

    extract_to = extract_to.resolve()
    total_uncompressed = 0

    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()

        for member in members:
            total_uncompressed += member.file_size
            if total_uncompressed > max_extracted_bytes:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Archive exceeds the maximum allowed uncompressed size.",
                )

            member_name = member.filename
            if member_name.startswith("/") or member_name.startswith("\\"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Rejected ZIP entry with absolute path: {member_name}",
                )

            # Resolve the final destination path and verify it stays
            # inside the extraction directory (Zip Slip protection).
            destination_path = (extract_to / member_name).resolve()
            try:
                destination_path.relative_to(extract_to)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Rejected ZIP entry with path traversal: {member_name}",
                )

            # Reject symlinks embedded in the archive outright - the
            # upper 16 bits of external_attr hold the Unix file mode
            # for archives created on POSIX systems.
            mode = (member.external_attr >> 16) & 0xFFFF
            is_symlink = mode != 0 and (mode & 0o170000) == 0o120000
            if is_symlink:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Rejected ZIP entry containing a symlink: {member_name}",
                )

        # All entries validated - now safe to extract.
        archive.extractall(extract_to, members=members)


def _find_gradlew(project_root: Path) -> Path:
    """
    Locate the gradlew wrapper script inside the extracted project.
    Handles projects zipped with an extra top-level folder, or with
    gradlew directly at the archive root.
    """
    # Prefer a gradlew at the shallowest depth.
    candidates = sorted(project_root.rglob("gradlew"), key=lambda p: len(p.parts))
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise BuildError(
        "No 'gradlew' wrapper script was found in the uploaded project. "
        "Make sure the ZIP includes the Gradle Wrapper (gradlew, gradlew.bat, "
        "and the gradle/wrapper directory) at the project root."
    )


def _find_apk(project_dir: Path) -> Path:
    """
    Locate the generated debug APK after a successful build.
    Searches the standard Gradle output location first, then falls
    back to a broader recursive search.
    """
    preferred_patterns = [
        "**/outputs/apk/debug/*.apk",
        "**/outputs/apk/**/*debug*.apk",
    ]
    for pattern in preferred_patterns:
        matches = sorted(project_dir.glob(pattern))
        if matches:
            return matches[0]

    # Fallback: any APK produced anywhere in the build output.
    all_apks = sorted(project_dir.rglob("*.apk"))
    if all_apks:
        return all_apks[0]

    raise BuildError(
        "Gradle reported a successful build, but no APK file could be located "
        "in the project's build/outputs directory."
    )


def _run_gradle_build(gradlew_path: Path) -> str:
    """
    Execute `./gradlew assembleDebug --no-daemon --stacktrace` in the
    directory containing gradlew, with a hard timeout. Returns combined
    stdout/stderr log on success. Raises BuildError with the captured
    log on failure or timeout.
    """
    project_dir = gradlew_path.parent
    gradlew_path.chmod(gradlew_path.stat().st_mode | 0o111)  # ensure executable

    env = os.environ.copy()
    env["ANDROID_SDK_ROOT"] = ANDROID_SDK_ROOT
    env["ANDROID_HOME"] = ANDROID_HOME
    env["JAVA_HOME"] = env.get("JAVA_HOME", "/usr/lib/jvm/java-17-openjdk-amd64")

    command = ["./gradlew", "assembleDebug", "--no-daemon", "--stacktrace"]

    logger.info("Starting Gradle build in %s", project_dir)
    start_time = time.time()

    try:
        result = subprocess.run(
            command,
            cwd=str(project_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=BUILD_TIMEOUT_SECONDS,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        partial_log = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        raise BuildError(
            f"Build timed out after {BUILD_TIMEOUT_SECONDS} seconds.",
            log=partial_log,
        )
    except FileNotFoundError:
        raise BuildError("gradlew could not be executed (interpreter or permissions issue).")

    elapsed = time.time() - start_time
    logger.info("Gradle build finished in %.1fs with exit code %s", elapsed, result.returncode)

    if result.returncode != 0:
        raise BuildError(
            f"Gradle build failed with exit code {result.returncode}.",
            log=result.stdout or "",
        )

    return result.stdout or ""


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "android-build-server"}


@app.post("/build")
async def build_apk(project_zip: UploadFile = File(...)):
    """
    Accept an Android project as a ZIP file, build it with the project's
    own Gradle Wrapper, and return the compiled debug APK.

    On build failure, returns HTTP 422 with the captured Gradle log so
    the client can surface a real, actionable error.
    """
    # --- Validate content type / filename hint ---------------------------
    filename = project_zip.filename or ""
    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file must be a .zip archive (field 'project_zip').",
        )

    workspace = _new_workspace()
    zip_path = workspace / "project.zip"
    extract_dir = workspace / "src"
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        # --- Save upload with enforced size limit -------------------------
        size = await _save_upload_with_limit(project_zip, zip_path, MAX_UPLOAD_BYTES)
        logger.info("Received upload '%s' (%d bytes)", filename, size)

        # --- Safe extraction (zip-slip / zip-bomb protected) --------------
        _safe_extract_zip(zip_path, extract_dir, MAX_EXTRACTED_BYTES)

        # The raw ZIP is no longer needed once extracted.
        zip_path.unlink(missing_ok=True)

        # --- Locate gradlew and build ---------------------------------------
        gradlew_path = _find_gradlew(extract_dir)
        build_log = _run_gradle_build(gradlew_path)

        # --- Locate the produced APK ---------------------------------------
        apk_path = _find_apk(gradlew_path.parent)

        logger.info("Build succeeded, returning APK at %s", apk_path)

        return FileResponse(
            path=str(apk_path),
            media_type="application/vnd.android.package-archive",
            filename="app-debug.apk",
            background=BackgroundTask(_cleanup_workspace, workspace),
        )

    except BuildError as exc:
        logger.warning("Build failed: %s", exc.message)
        _cleanup_workspace(workspace)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "status": "build_failed",
                "error": exc.message,
                "log": exc.log[-20000:] if exc.log else "",  # cap log size returned
            },
        )
    except HTTPException:
        _cleanup_workspace(workspace)
        raise
    except Exception as exc:  # noqa: BLE001 - convert unexpected errors to a clean 500
        logger.exception("Unexpected error during build")
        _cleanup_workspace(workspace)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal build server error: {exc}",
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        reload=False,
    )
