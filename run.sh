docker run -d \
  --name choi-bot \
  --env-file ini.env \
  -e TZ=Asia/Seoul \
  -v $(pwd)/logs:/app/logs \
  choi-bot

