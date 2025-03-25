#!/bin/bash

# 설정 값
REPO_DIR="$HOME/choi_bot"
BOT_SCRIPT="choi_bot.py"
GIT_BRANCH="main"

# 1. 저장소 업데이트
echo "🔄 GitHub에서 최신 코드 가져오는 중..."
cd $REPO_DIR
git fetch origin $GIT_BRANCH
git reset --hard origin/$GIT_BRANCH

# 2. 봇 프로세스 종료
echo "🛑 기존 봇 종료 중..."
pkill -f "$BOT_SCRIPT"

# 3. 봇 실행 (백그라운드 실행)
echo "🚀 봇 재시작!"
nohup python3 $REPO_DIR/$BOT_SCRIPT > bot.log 2>&1 &

echo "✅ 업데이트 및 재시작 완료!"
