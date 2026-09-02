#!/bin/bash
# HiperGlobal Reporter — One-command setup
# Run: bash setup.sh

set -e
INSTALL_DIR="$HOME/Documents/dev_Ops_practice/install_report"
PLIST_NAME="com.uveye.hiperglobal-reporter"
PLIST_SRC="$(dirname "$0")/$PLIST_NAME.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

echo ""
echo "════════════════════════════════════════════"
echo "  HiperGlobal Reporter Setup"nana
echo "════════════════════════════════════════════"
echo ""

# 1. Create install directory
echo "→ Creating $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

# 2. Copy script files
echo "→ Copying scripts"
cp "$(dirname "$0")/hiperglobal_reporter.py" "$INSTALL_DIR/"
cp "$(dirname "$0")/.env.template"           "$INSTALL_DIR/.env.template"

# 3. Copy dashboard HTML (if not already there)
DASH="$INSTALL_DIR/pu_dashboard.html"
if [ ! -f "$DASH" ]; then
  if [ -f "$(dirname "$0")/pu_dashboard.html" ]; then
    cp "$(dirname "$0")/pu_dashboard.html" "$DASH"
    echo "→ Dashboard HTML copied to $DASH"
  else
    echo "⚠  No dashboard HTML found — copy pu_dashboard.html to $DASH manually"
  fi
else
  echo "→ Dashboard already exists at $DASH"
fi

# 4. Create .env if not present
if [ ! -f "$INSTALL_DIR/.env" ]; then
  cp "$INSTALL_DIR/.env.template" "$INSTALL_DIR/.env"
  echo ""
  echo "⚠  IMPORTANT: Fill in your credentials:"
  echo "   open $INSTALL_DIR/.env"
  echo ""
else
  echo "→ .env already exists — keeping it"
fi

# 5. Install Python dependencies
echo "→ Installing Python dependencies"
pip3 install requests python-dotenv --break-system-packages --quiet

# 6. Install launchd agent
echo "→ Installing launchd agent"
cp "$PLIST_SRC" "$PLIST_DEST"
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load   "$PLIST_DEST"

echo ""
echo "════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. Edit credentials:  open $INSTALL_DIR/.env"
echo "  2. Test run:          python3 $INSTALL_DIR/hiperglobal_reporter.py --dry-run"
echo "  3. Live test:         python3 $INSTALL_DIR/hiperglobal_reporter.py"
echo ""
echo "  Schedule: 1st of every month at 9:00 AM"
echo "  Log:      $INSTALL_DIR/reporter.log"
echo "════════════════════════════════════════════"
echo ""
