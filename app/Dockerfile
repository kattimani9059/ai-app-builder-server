########################################################################
# AI App Builder Pro - Android APK Build Server
# Ubuntu 24.04 + OpenJDK 17 + Android SDK (Platform 35 / Build-Tools 35.0.0)
########################################################################
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# ---------------------------------------------------------------------
# System packages: JDK 17, Python 3, unzip/curl for SDK provisioning,
# and the 32-bit libs some Android SDK binaries still expect.
# ---------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jdk-headless \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    unzip \
    ca-certificates \
    git \
    libc6-i386 \
    libncurses5:amd64 \
    libstdc++6:amd64 \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# ---------------------------------------------------------------------
# Android SDK Command Line Tools
# ---------------------------------------------------------------------
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV ANDROID_HOME=/opt/android-sdk

RUN mkdir -p ${ANDROID_SDK_ROOT}/cmdline-tools

ARG CMDLINE_TOOLS_URL="https://dl.google.com/android/repo/commandlinetools-linux-11076708_latest.zip"

RUN curl -fSL -o /tmp/cmdline-tools.zip "${CMDLINE_TOOLS_URL}" \
    && unzip -q /tmp/cmdline-tools.zip -d ${ANDROID_SDK_ROOT}/cmdline-tools \
    && mv ${ANDROID_SDK_ROOT}/cmdline-tools/cmdline-tools ${ANDROID_SDK_ROOT}/cmdline-tools/latest \
    && rm /tmp/cmdline-tools.zip

ENV PATH="${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin:${ANDROID_SDK_ROOT}/platform-tools:${PATH}"

# Accept all SDK licenses non-interactively, then install the required
# SDK components: platform-tools, Android Platform 35, Build-Tools 35.0.0.
RUN yes | sdkmanager --sdk_root=${ANDROID_SDK_ROOT} --licenses > /dev/null || true \
    && sdkmanager --sdk_root=${ANDROID_SDK_ROOT} \
        "platform-tools" \
        "platforms;android-35" \
        "build-tools;35.0.0" \
    && yes | sdkmanager --sdk_root=${ANDROID_SDK_ROOT} --licenses > /dev/null || true

ENV PATH="${ANDROID_SDK_ROOT}/build-tools/35.0.0:${PATH}"

# ---------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------
WORKDIR /srv/app

COPY requirements.txt /srv/app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /srv/app/requirements.txt

COPY app /srv/app/app

# Directory used for ephemeral per-build workspaces.
RUN mkdir -p /srv/app/build-workspace

EXPOSE 8080

ENV PORT=8080

# Use shell form so ${PORT} (injected by Railway at runtime) is honored,
# falling back to 8080 when it is not set.
CMD ["sh", "-c", "python3 -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
