docker stop choi-bot 2>/dev/null

docker rm choi-bot 2>/dev/null

docker build -t choi-bot .

docker run -d \
  --name choi-bot \
  --env-file ini.env \
  --restart always \
  -e TZ=Asia/Seoul \
  -v $(pwd)/logs:/app/logs \
  choi-bot

