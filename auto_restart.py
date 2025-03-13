from watchdog.observers.polling import PollingObserver  # ✅ Polling 방식 사용
from watchdog.events import FileSystemEventHandler
import os
import time
import subprocess

# ✅ 실행할 Python 파일 및 tmux 세션명
BOT_FILE = "choi_bot.py"
TMUX_SESSION = "discord"

class BotRestartHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.src_path.endswith(BOT_FILE):
            print(f"[INFO] {BOT_FILE} 파일 변경 감지! 봇 재시작 중...")

            # ✅ 기존 실행 중인 봇 종료
            subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-c"], check=False)
            time.sleep(2)  # 프로세스 종료 대기

            # ✅ 새로운 봇 실행
            subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, f"python3 {BOT_FILE}", "Enter"], check=False)

            print("[INFO] 디스코드 봇 재시작 완료!")

if __name__ == "__main__":
    event_handler = BotRestartHandler()
    observer = PollingObserver()  # ✅ Polling 방식으로 변경
    observer.schedule(event_handler, path=os.getcwd(), recursive=False)
    observer.start()
    
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()