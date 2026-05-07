#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[x]${NC} $1"; exit 1; }

echo "============================================"
echo " Anime4K Web Upscaler — Installation"
echo "============================================"
echo ""

# Check OS
if [ "$(uname -s)" != "Linux" ]; then
    err "This installer is for Linux only."
fi

# Check CUDA
log "Checking for NVIDIA GPU + CUDA..."
if ! command -v nvidia-smi &>/dev/null; then
    err "nvidia-smi not found. Install NVIDIA drivers + CUDA toolkit first."
fi
nvidia-smi --query-gpu=name --format=csv,noheader | head -1

# Check FFmpeg
log "Checking FFmpeg..."
if ! command -v ffmpeg &>/dev/null; then
    warn "ffmpeg not found. Installing..."
    if command -v apt &>/dev/null; then
        sudo apt update && sudo apt install -y ffmpeg libavcodec-dev libavformat-dev libavutil-dev libswscale-dev
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y ffmpeg ffmpeg-devel
    else
        err "Cannot auto-install ffmpeg. Please install it manually."
    fi
else
    ffmpeg -version | head -1
fi

# Check for build tools
log "Checking build tools..."
for tool in cmake g++ make; do
    if ! command -v $tool &>/dev/null; then
        warn "$tool not found. Installing..."
        if command -v apt &>/dev/null; then
            sudo apt install -y build-essential cmake
        elif command -v dnf &>/dev/null; then
            sudo dnf groupinstall -y "Development Tools" && sudo dnf install -y cmake
        else
            err "Need $tool. Please install it manually."
        fi
        break
    fi
done

# Check if ac_cli already exists
ANIME4KCPP_DIR="$HOME/anime4kcpp"
if [ -x "$ANIME4KCPP_DIR/build/bin/ac_cli" ]; then
    log "ac_cli already installed at $ANIME4KCPP_DIR/build/bin/ac_cli"
    read -rp "Recompile? [y/N] " recompile
    if [ "$recompile" != "y" ] && [ "$recompile" != "Y" ]; then
        log "Skipping Anime4KCPP build."
        log "Installation complete!"
        echo ""
        echo "Add to PATH: export PATH=\"$ANIME4KCPP_DIR/build/bin:\$PATH\""
        echo "Then: cd $(pwd) && source venv/bin/activate && uvicorn server:app --host 0.0.0.0 --port 8000"
        exit 0
    fi
fi

# Build Anime4KCPP
log "Cloning and building Anime4KCPP..."
mkdir -p "$ANIME4KCPP_DIR"
cd "$ANIME4KCPP_DIR"

if [ -f "CMakeLists.txt" ]; then
    log "Anime4KCPP source already present, pulling latest..."
    git pull || true
else
    git clone https://github.com/TianZerL/Anime4KCPP.git .
fi

mkdir -p build && cd build

log "Running CMake (with CUDA + video support)..."
cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DAC_CORE_WITH_CUDA=ON \
    -DAC_BUILD_CLI=ON \
    -DAC_BUILD_VIDEO=ON \
    -DAC_BUILD_GUI=OFF \
    -DCMAKE_CUDA_ARCHITECTURES="75;80;86;89;90"

log "Building (this will take a few minutes)..."
cmake --build . --config Release -j"$(nproc)"

if [ ! -x "bin/ac_cli" ]; then
    err "Build failed: ac_cli not found in build/bin/"
fi

log "ac_cli built successfully."
"$(pwd)/bin/ac_cli" -v || warn "ac_cli -v returned non-zero, but binary exists"

echo ""
log "============================================"
log " Installation complete!"
log "============================================"
echo ""
echo "Add this to your PATH or use full path:"
echo "  export PATH=\"$ANIME4KCPP_DIR/build/bin:\$PATH\""
echo ""
echo "Then start the server:"
echo "  cd $(pwd)"
echo "  source venv/bin/activate"
echo "  uvicorn server:app --host 0.0.0.0 --port 8000"
