# AI App Builder Pro — Android APK Build Server

A production-ready FastAPI service that **actually compiles** Android
projects into installable APK files. Upload a project ZIP, the server
extracts it, runs the project's own Gradle Wrapper (`./gradlew
assembleDebug`) inside a container that already has OpenJDK 17 and the
Android SDK installed, and returns the real, compiled `app-debug.apk`.

There is no mock build step: `/build` invokes Gradle and returns either
the generated APK or the actual Gradle failure log.

---

## Project structure

```
ai-app-builder-server/
├── Dockerfile
├── railway.toml
├── requirements.txt
├── README.md
└── app/
    ├── __init__.py
    └── main.py
```

---

## Tech stack

- Python 3 / FastAPI / Uvicorn
- Docker (Ubuntu 24.04 base image)
- OpenJDK 17
- Android SDK Command Line Tools
- Android SDK Platform 35
- Android Build Tools 35.0.0
- Gradle Wrapper (supplied by each uploaded Android project — not bundled)

---

## API

### `GET /health`

Health check.

**Response `200`:**
```json
{
  "status": "ok",
  "service": "android-build-server"
}
```

### `POST /build`

Builds an uploaded Android project and returns the compiled APK.

- Content type: `multipart/form-data`
- Field name: `project_zip` (a `.zip` file)
- Max upload size: **100 MB**
- Max build time: **10 minutes**

**Success — `200 OK`**
Body: raw APK bytes.
- `Content-Type: application/vnd.android.package-archive`
- `Content-Disposition: attachment; filename="app-debug.apk"`

**Build failure — `422 Unprocessable Entity`**
```json
{
  "status": "build_failed",
  "error": "Gradle build failed with exit code 1.",
  "log": "... full captured Gradle stdout/stderr (tail, up to 20000 chars) ..."
}
```

**Bad request (not a zip, path traversal, oversized archive) — `400 Bad Request`**
```json
{ "detail": "Uploaded file must be a .zip archive (field 'project_zip')." }
```

**Upload too large — `413 Request Entity Too Large`**
```json
{ "detail": "Uploaded project exceeds the 100 MB limit." }
```

**Unexpected server error — `500 Internal Server Error`**
```json
{ "detail": "Internal build server error: <details>" }
```

---

## Expected Android project ZIP structure

The ZIP must contain a buildable Gradle Android project **including its
Gradle Wrapper**. A single top-level folder is fine — the server finds
`gradlew` automatically at whatever depth it lives.

```
MyAndroidApp.zip
└── MyAndroidApp/                 <- top-level folder (optional, either works)
    ├── gradlew                   <- REQUIRED
    ├── gradlew.bat
    ├── gradle/
    │   └── wrapper/
    │       ├── gradle-wrapper.jar
    │       └── gradle-wrapper.properties
    ├── settings.gradle(.kts)
    ├── build.gradle(.kts)
    ├── local.properties          <- optional; server sets ANDROID_HOME/ANDROID_SDK_ROOT itself
    └── app/
        ├── build.gradle(.kts)
        └── src/
            └── main/
                ├── AndroidManifest.xml
                ├── java/ or kotlin/
                └── res/
```

Notes:
- **Do not** rely on a checked-in `local.properties` pointing at some
  local machine's SDK path — the container's `ANDROID_HOME` /
  `ANDROID_SDK_ROOT` are exported into the Gradle process environment,
  which Android Gradle Plugin honors automatically.
- The Gradle Wrapper's declared Gradle version must be compatible with
  Android Gradle Plugin builds against **compileSdk 35** / **Build
  Tools 35.0.0** (AGP 8.x + Gradle 8.x is recommended).
- The wrapper JAR (`gradle/wrapper/gradle-wrapper.jar`) must be present
  in the ZIP — the server does not generate it for you.

---

## Running locally (without Docker)

Requires: Python 3.11+, OpenJDK 17, and a local Android SDK with
Platform 35 + Build-Tools 35.0.0 installed.

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Point the server at your local Android SDK
export ANDROID_SDK_ROOT=$HOME/Android/Sdk
export ANDROID_HOME=$HOME/Android/Sdk

# 3. Run the server
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Verify it's up:
```bash
curl http://localhost:8080/health
```

---

## Building the Docker image

```bash
docker build -t ai-app-builder-server .
```

This image installs everything needed to build Android apps —
OpenJDK 17, the Android SDK Command Line Tools, `platform-tools`,
`platforms;android-35`, and `build-tools;35.0.0` — so no Android
Studio or AIDE installation is ever required, on the server or the
client.

## Running the container

```bash
docker run --rm -p 8080:8080 ai-app-builder-server
```

The server listens on port `8080` by default, or on `$PORT` if it is
set in the environment (used automatically by Railway).

---

## Example curl request

```bash
curl -X POST http://localhost:8080/build \
  -F "project_zip=@/path/to/MyAndroidApp.zip" \
  -o app-debug.apk \
  -w "HTTP %{http_code}\n"
```

- If the build succeeds, `app-debug.apk` is written to disk and you can
  install it with `adb install app-debug.apk`.
- If the build fails, curl still writes the JSON error body to
  `app-debug.apk` — inspect it with `cat app-debug.apk` to see the
  Gradle log, or drop `-o` and let curl print the JSON directly:

```bash
curl -X POST http://localhost:8080/build \
  -F "project_zip=@/path/to/MyAndroidApp.zip"
```

---

## Deploying to Railway

This repo includes a `railway.toml` pre-configured for a Dockerfile
deployment.

1. Push this repository to GitHub (or use the Railway CLI directly).
2. In Railway, create a new project → **Deploy from GitHub repo** (or
   run `railway up` from this directory with the CLI).
3. Railway detects `railway.toml` and:
   - Builds the image from the `Dockerfile`.
   - Starts the container with
     `python3 -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}`,
     binding to Railway's injected `$PORT`.
   - Polls `GET /health` as the deployment healthcheck.
   - Restarts the service automatically on failure
     (`restartPolicyType = "ON_FAILURE"`, up to 10 retries).
4. Once deployed, your build endpoint is available at:
   ```
   https://<your-railway-domain>/build
   ```

No additional environment variables are required — `ANDROID_SDK_ROOT`
and `ANDROID_HOME` are baked into the image at `/opt/android-sdk`.

---

## Security measures implemented

| Concern | Mitigation |
|---|---|
| Oversized uploads | Streamed to disk in 1 MB chunks; aborted at 100 MB |
| Zip bombs | Cumulative uncompressed size capped at 1 GB during extraction |
| Zip Slip / path traversal | Every archive member's resolved path is verified to stay inside the extraction directory before extraction; absolute paths and embedded symlinks are rejected outright |
| Runaway builds | `subprocess.run(..., timeout=600)` hard-kills builds after 10 minutes |
| Disk exhaustion / data leakage | Every build runs in a unique `tempfile.mkdtemp` workspace; the uploaded ZIP is deleted immediately after extraction; the entire workspace (source, build outputs, APK) is removed via a `BackgroundTask` right after the HTTP response is fully sent, on both success and failure paths |
| Uninformative errors | Failed builds return HTTP 422 with the real captured Gradle stdout/stderr so a client app can show the actual compiler/lint error |

No uploaded project ZIP or build artifact is ever written outside its
single-use temporary workspace, and nothing is retained after the
request completes.
