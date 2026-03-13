#!/bin/bash
# Quick start script for local testing

echo "🚀 ChatbotRadar - Local Development Setup"
echo "=========================================="
echo ""

# Check Python version
echo "📌 Checking Python version..."
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version)
    echo "✅ Found: $PYTHON_VERSION"
else
    echo "❌ Python 3 not found. Please install Python 3.11+"
    exit 1
fi

echo ""
echo "📦 Installing dependencies..."
pip install -r requirements.txt

echo ""
echo "🔑 Generating secret key..."
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
export SECRET_KEY=$SECRET_KEY
echo "✅ Secret key generated"

echo ""
echo "🌐 Starting Flask development server..."
echo ""
echo "Your app will be available at: http://localhost:5001"
echo "Press CTRL+C to stop the server"
echo ""
echo "=========================================="
echo ""

python3 app.py
