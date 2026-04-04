#!/bin/bash
set -e

echo "📦 Packaging Lambda function..."

# Clean previous build
rm -rf build/ lambda.zip

# Install dependencies into build/ targeting Lambda's Linux x86_64 runtime
uv pip install . --target build/ --python-platform x86_64-unknown-linux-gnu --python 3.12 --quiet

# Copy source code
cp src/*.py build/

# Create ZIP
cd build
zip -r ../lambda.zip . --quiet
cd ..

echo "✅ Created lambda.zip ($(du -h lambda.zip | cut -f1))"
echo ""
echo "Deploy with:"
echo "  aws lambda update-function-code --function-name finance-tracker --zip-file fileb://lambda.zip"
