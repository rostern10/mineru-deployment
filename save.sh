#!/bin/bash
# 版本管理
DIR="/home/szssyyy/mineru-deployment"
VER="${DIR}/versions/$(date +%Y%m%d_%H%M)"
mkdir -p "$VER"
cp "$DIR/bridge.py" "$DIR/index.html" "$DIR/compose.yaml" "$VER/"
echo "✅ 已保存到 versions/$(basename $VER)/"
echo ""
echo "历史版本："
ls -d "$DIR/versions/"*/ | xargs -I{} basename {} | sort -r | head -10
