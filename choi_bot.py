import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks
from discord.ui import View, Button, Modal, TextInput, Select
import os
from collections import deque
import time
import asyncio
from datetime import datetime
import re

from bot.settings import Settings, load_settings, validate_settings
from bot.llm.contracts import LLMError, LLMRequest, Message
from bot.llm.router import LLMRouter, task_policies
from bot.conversation import ConversationQueue
from bot.persona import (CHARACTER_PROMPT, COMMAND_PERSONA, VOICE_ONLY,
                         build_conversation_prompt)
from bot.discord_output import (send, edit, loading, channel_send, progress_edit,
                                progress_send)
from types import SimpleNamespace


#환경 변수 및 상수
MAX_DIALOGS = 20 #대화 맥락 포함 이전 대화 수
CONTEXT_EXPERATION = 120 #대화 맥락 유지 시간
BUILD_VERSION = "1.8.1" #최씨 봇 버전
ALLOWED_CH = {1383015103926112296, 1348180197714821172, 0} #허용된 대화 채널 ID
ANNOUNCEMENT_CH = 1348180197714821172 #공지 올릴 대화 채널 ID
ANNOUNCEMENT_TIME = 43200 #공지 올릴 시간
CHECK_CONTEXT_TIME = 30 #맥락 체크 타이밍
MODEL = "gemini-3.5-flash-lite" #모델
now = datetime.fromtimestamp(time.time()).strftime("%Y.%m.%d %H:%M:%S") #현재시각
KEY_WORDS = ["최씨", "영원"] #감지 키워드
reset_flag = 0
DEP_TIME = datetime(2025, 3, 4, 4, 30, 00) #최씨가 떠나간 시간
RET_TIME = datetime(2025, 7, 15, 21, 57, 00) #최씨가 돌아온 시간
ROLE_WHITELIST = {
    1408453125723127928, # 메이플
    1408453271445831711, # 로아
    1463770679546482819, # 허리피라우
    1467485314573406230, # 증바람
    1483643794162323536, # 발로란트
    1482746732008837200, # 원신
    1437066578658066616, # 배그
}
GUILD_ID = 1277993256927498260 #서버 아이디

nowmodel = MODEL #현재 모델

#특정 날짜와 현재 시간까지 경과한 
def time_since(event_time):
    nowtime = datetime.now()
    elapsed_time = nowtime - event_time

    days = elapsed_time.days
    hours, remainder = divmod(elapsed_time.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    return f"{days}일 {hours}시간 {minutes}분 {seconds}초"
leave_time = time_since(DEP_TIME)
return_time = time_since(RET_TIME) 

stopflag = 0 #API 요청 과부하로 중지 여부 (0: 재개, 1: 중지)


#최씨 봇 정보 및 유저 아이디 매핑
USER_MAP = {
    "jhy.jng": "주효중",
    "jhy.false": "주효중",
    "choiyeongweon_": "주효중",
    "hmeojidai": "김두멍",
    "tokach_2": "지성게이",
    "luna8810": "따이호",
    "hiyom_1105": "김민트",
    "soeun_0517": "문도",
    "tokach_": "지성게이",
    "sakura_0401_": "김유리",
    "newhead": "성탄종",
    "s1b1ltaeng": "서민수",
    "jun_xx_": "박준혁",
    "apwnel": "메뚜기",
    "1killcut": "호영게이",
    "zzin_bbangso": "조둥",
    "mo3064": "이충선",
    "taemin_park": "박태민"
}

INFORMATION = f"""
**:robot: 미래 가젯 최씨 봇(가칭) 버전:{BUILD_VERSION} Made by jhy.jng**
```
제공되는 모든 답변은 Google Gemini 2.5에 기반합니다.
Generative AI 기능 사용을 위해, 본 서버의 모든 대화 로그를 수집합니다.
대화에 참여하면 User ID와 대화 내용을 수집하는 것에 동의한 것으로 간주됩니다.
봇 실행 시각: {now}
Version: {BUILD_VERSION}```
"""
WHO_AM_I = f"""
# 최씨 (본명 최 영 원)
- 나이: 85세 + a
- 성별: 남성, ***GAY***
- 본캐: 백전노장할아브, 버서커, 젤리젤리
```
자세한 내용은 위대한 젤리젤리 전설 참고.
```
## 인간관계
- 남편: 김두멍
- 아들: 박주녁
- 친구: 마효중, 김민트, 지성게이, 저사구, 서민수, 조둥, 유링게슝, 메뚜기, 호영게이
- 전우애: 박태민
- 싸가지없는X: 문도
- 유기: 성탄종
"""
#패치노트 사항
PATCHNOTE = f"""
# 최씨 봇 {BUILD_VERSION} 버전 개발자 노트
{BUILD_VERSION} 버전의 **주요 업데이트 사항**
## Docker 환경으로 이전
- 최씨 봇은 이제 Docker 환경에서 실행됩니다.
## 최신 API 지원
- 이제 최씨 봇은, 최신 Discord API가 지원됩니다.
- 명령어 자동완성 기능 등, 사용성이 개선되었습니다.
- 최신 API를 활용한 다양한 기능 추가 예정입니다.
## 번역기 기능 추가
- 이제 최씨 봇 번역기를 이용할 수 있습니다.
- `/번역` 명령어를 통해 번역기 UI를 띄울 수 있습니다
- 언어를 먼저 선택한 후, 문장 입력 버튼을 누르면 제출란이 뜹니다.
- 제출란에 번역할 문장을 넣고, 저장합니다.
- 번역 버튼을 누르면 번역 결과를 출력해줍니다.
``` 수정 사항
1. discord.app_commands 적용
2. 적용 중 채팅 버그를 수정했습니다 (1.7.1)
3. 요약, 찾기 명령어 코드 리팩터링 (1.7.2)
4. 버그 수정 (1.7.4)
5. 번역기 기능 추가 (1.7.5)
6. Docker 환경으로 이전 (1.8.0)
```
"""

#캐릭터 프롬프트와 말투 규칙은 bot/persona.py가 관리한다.

# Runtime dependencies are created explicitly by initialize_runtime(), never import.
API_KEYS = ()
client = None
tree = None
llm_router = None


async def generate_content_timeout(prompt, timeout=None, *, task_type):
    if llm_router is None:
        raise RuntimeError("LLM runtime is not initialized")
    return await llm_router.generate(LLMRequest(
        task_type=task_type, messages=(Message("user", prompt),), timeout=timeout,
    ))


#Log folder
LOG_FOLDER = "logs"

def save__logs(user, msg):
    today = datetime.now().strftime("%Y-%m-%d")
    log_filename = os.path.join(LOG_FOLDER, f"{today}.txt")

    log_entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {user}: {msg}\n"

    with open(log_filename, "a", encoding="utf-8") as log_file:
        log_file.write(log_entry)


def get_latest_log_lines(count: int):
    if count <= 0:
        return None, []

    if not os.path.exists(LOG_FOLDER):
        return None, []

    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}\.txt$")
    log_files = [name for name in os.listdir(LOG_FOLDER) if pattern.match(name)]

    if not log_files:
        return None, []

    latest_file = max(log_files)
    latest_path = os.path.join(LOG_FOLDER, latest_file)

    with open(latest_path, "r", encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f]

    return latest_file, lines[-count:]


#최근 대화 참여자 목록
active_users = set()

#최근 대화 저장 
conversation_context = deque(maxlen=MAX_DIALOGS)

#마지막 대화 시간 저장
last_conversation_time = 0

#최근 대화 내역 저장, 사용자 맥락
def update_context(user, message):
    global last_conversation_time
    conversation_context.append(f"{user}: {message}")
    active_users.add(user)
    last_conversation_time = time.time()

#최근 대화 내역 가져오기
def get_context():
    return "\n".join(conversation_context)

#최근 대화 참여자가 존재하고, 2분 이내면 True 반환
def is_alive():
    global last_conversation_time
    elapsed_time = time.time() - last_conversation_time
    if elapsed_time > CONTEXT_EXPERATION:
        return False
    return len(active_users) > 0 

#대화 맥락 초기화
last_reset_time = 0
async def clear_context(arg = "Auto"):
    global conversation_context, active_users, reset_flag
    conversation.reset("natural" if arg == "Finite Context" else arg)
    conversation_context.clear()
    active_users.clear()
    reset_flag = 1

    channel = client.get_channel(ANNOUNCEMENT_CH)
    if channel:
        texts = f"`Conversation context initialized. = {arg}`"
        try:
            await channel.send(texts)
        except Exception:
            print("[WARN] Context reset notice could not be sent")
    console_log = f"[DEBUG] 대화 맥락 초기화됨: {arg}"
    print(console_log)
    #save__logs("Console", console_log)

#최씨가 불렸는지 확인인
def is_called(message:str):
    call_pattern = KEY_WORDS
    if "최씨" in message:
        if any(pattern in message for pattern in call_pattern):
            return True
        if message.startswith("최씨"):
            return True
    return False

async def on_ready(): #Start client
    guild = discord.Object(id=GUILD_ID)
    tree.clear_commands(guild=guild)
    await tree.sync(guild=guild)
    synced = await tree.sync()
    print(f"✅ 최씨 봇 준비 완료! {client.user}- 등록된 명령어 수: {len(synced)}")
    await client.change_presence(activity=discord.Game("잉! 잉! 안 나가!"))
    for loop in (send_announcement, check_context, send_waist):
        if not loop.is_running():
            loop.start()

@tasks.loop(seconds=ANNOUNCEMENT_TIME) #Announcement
async def send_announcement():
    #announce specific time
    global leave_time
    leave_time = time_since(DEP_TIME)
    channel = client.get_channel(ANNOUNCEMENT_CH)
    if channel:
        await channel.send(INFORMATION)
    else: print("Error")


@tasks.loop(seconds=3600) # 허리피라우
async def send_waist():
    channel = client.get_channel(1348180197714821172)
    if channel is None:
        try:
            channel = await client.fetch_channel(1348180197714821172)
        except Exception as e:
            print(f"[ERROR] 채널을 불러오지 못했습니다: {str(e)}")
            return
    guild = channel.guild
    role = guild.get_role(1463770679546482819)
    if role is None:
        print("[ERROR] 역할을 불러오지 못했습니다.")
        return
    online_members = [
        m for m in role.members
        if (not m.bot) and (m.status != discord.Status.offline)
    ]

    if not online_members:
        print("[DEBUG] 온라인 허리피라우 대상자가 없습니다.")
        return
    
    print(f"[DEBUG] 허리피라우 알림 대상자: {online_members}")
    mentions = " ".join(member.mention for member in online_members)
    await channel.send(
        content=f"{mentions} 허리요정 최씨입니다. 바른 자세를 유지하세요!", 
        allowed_mentions=discord.AllowedMentions(users=online_members, roles=False, everyone=False)
    )

@tasks.loop(seconds = CHECK_CONTEXT_TIME) #Check contexts
async def check_context():
    global last_reset_time
    if not is_alive():
        if (last_reset_time == 0 or (time.time() - last_reset_time) > CONTEXT_EXPERATION) and reset_flag == 0:
            console_log = f"[DEBUG] 맥락 자동 초기화 실행 (last_reset_time={last_reset_time})"
            print(console_log)
            #save__logs("Console", console_log)
            await clear_context()
            last_reset_time = time.time()
        else:
            console_log = f"[DEBUG] 이미 초기화됨 (last_reset_time={last_reset_time}, 경과 시간={time.time() - last_reset_time})"
            print(console_log)
            #save__logs("Console", console_log)
            
    else:
        console_log = f"[DEBUG] 맥락 대기중 (last_reset_time={last_reset_time}, 경과 시간={time.time() - last_reset_time})"
        print(console_log)
        #save__logs("Console", console_log)


@send_announcement.before_loop
async def before_announcement():
    """ 봇이 완전히 실행된 후 루프를 시작하도록 설정 """
    await client.wait_until_ready()

async def on_application_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.CommandInvokeError):
        await send(interaction, "명령어가 올바르지 않거나, 오류가 발생했습니다.")
        print(str(error))
        return
    elif isinstance(error, app_commands.errors.MissingPermissions):
        await send(interaction, "`Permission Denied.`")
        print(str(error))
        return
    else:
        await send(interaction, "침입자 발견, 자가방어시스템을 가동합니다.")
        print(str(error))

def record_bot_reply(text):
    try:
        save__logs("최씨 봇", text)
    except OSError:
        print("[WARN] Bot reply log write failed; generation will not be repeated")


#답변 출력 함수
async def reply(message, response, epoch=None):
    epoch = conversation.epoch if epoch is None else epoch
    valid = lambda: conversation.valid(epoch)
    if not valid():
        return
    reply_text = response.text if response.text is not None else "응애! 대답할 수 없음!"
    if "마이크 끄는 소리" in reply_text:
        if await channel_send(message.channel, reply_text, valid=valid):
            record_bot_reply(reply_text)
            await clear_context("Finite Context")
        return
    if "00100" not in reply_text:
        if not await channel_send(message.channel, reply_text, valid=valid):
            return
    if valid():
        record_bot_reply(reply_text)
        update_context("최씨 봇", reply_text)


async def on_message(message):
    if message.author == client.user:
        return
    save__logs(message.author.name, message.content)
    if message.channel.id not in ALLOWED_CH:
        return
    snapshot = SimpleNamespace(content=str(message.content), channel=message.channel,
                               author=SimpleNamespace(name=str(message.author.name)))
    try:
        await conversation.submit(snapshot, is_called(snapshot.content))
    except asyncio.QueueFull:
        await channel_send(message.channel, "대화 요청이 많아 잠시 후 다시 불러주세요.")


async def process_conversation_message(message, epoch):
    if not conversation.valid(epoch):
        return
    if conversation_context and not is_alive():
        epoch = conversation.epoch + 1
        await clear_context("Expired")
        if not conversation.valid(epoch):
            return

    new_conversation = not conversation_context and is_called(message.content)
    if not (new_conversation or (conversation_context and is_alive())):
        return

    global reset_flag
    if new_conversation:
        conversation_context.clear() #initialize context
        active_users.clear() #init users
    try:
        reset_flag = 0
        user_id = str(message.author.name)
        real_name = USER_MAP.get(user_id, user_id)
        msg = str(message.content)
        # Snapshot before recording: the model must see this utterance exactly once,
        # in [현재 발언], never also inside [이전 대화].
        history = list(conversation_context)
        update_context(real_name, msg)
        print(f"{real_name}: {msg}\n")
        response = await generate_content_timeout(
            build_conversation_prompt(history, real_name, msg,
                                      new_conversation=new_conversation),
            task_type="chat")
        await reply(message, response, epoch)
        print(conversation_context)

    except Exception as e:
        if conversation.valid(epoch):
            await channel_send(message.channel, f"잉! 잘못된 명령 발생! {str(e)}",
                               valid=lambda: conversation.valid(epoch))

#Commands

@app_commands.command(name="test", description="test message.")
async def test(interaction: discord.Interaction):
    await send(interaction, "Test Message")


@app_commands.command(name="로그", description="최신 로그에서 n개의 채팅 로그를 불러옵니다.")
@app_commands.describe(n="가져올 로그 개수 (1~100)")
async def 로그(interaction: discord.Interaction, n: int):
    if n < 1:
        await send(interaction, "1 이상의 정수를 입력해주세요.", ephemeral=True)
        return

    if n > 100:
        n = 100

    latest_file, lines = get_latest_log_lines(n)
    if latest_file is None:
        await send(interaction, "불러올 로그 파일이 없습니다.", ephemeral=True)
        return

    if not lines:
        await send(interaction, f"`{latest_file}` 파일에 표시할 로그가 없습니다.")
        return

    chunks = []
    current = ""
    max_len = 1700

    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_len:
            if current:
                chunks.append(current)
                current = line
            else:
                for i in range(0, len(line), max_len):
                    chunks.append(line[i:i + max_len])
                current = ""
        else:
            current = candidate

    if current:
        chunks.append(current)

    header = f"# 최신 로그: `{latest_file}`\n최근 {len(lines)}개 로그"
    await send(interaction, f"{header}\n```\n{chunks[0]}\n```")

    for chunk in chunks[1:]:
        await send(interaction, f"```\n{chunk}\n```")


@app_commands.command(name="config", description="config settings")
@app_commands.checks.has_permissions(administrator=True)
async def config(interaction: discord.Interaction, command: str, value: str = None, args: str = None):
    global stopflag
    print(command)
    if command == None:
        print("No command provided")
    if command == "summary":
        if value == 'True':
            stopflag = 0
            await send(interaction, "`요약 기능 활성화`")
            return
        elif value == 'False':
            stopflag = 1
            await send(interaction, "`요약 기능 비활성화`")
            return
        else: 
            await send(interaction, "`명령어 인수, 혹은 명령어가 잘못되었습니다. (Help to !config help)`")
        return
    elif command == "user":
        if value is None:
            await send(interaction, f"```유저 ID 매핑 {USER_MAP}```")
            return
        elif value in USER_MAP:
            if args == "delete":
                del USER_MAP[value]
                await send(interaction, f"`유저 ID 매핑 삭제: {value}`")
                return
            if args is None:
                await send(interaction, f"`User ID {value}의 이름: {USER_MAP[value]}`")
                return
            USER_MAP[value] = args
            await send(interaction, f"`기존 유저 ID 매핑 업데이트: {value} -> {args}`")
            return
        elif value not in USER_MAP and args != None:
            USER_MAP[value] = args
            await send(interaction, f"`신규 유저 ID 매핑: {value} -> {args}`")
            return
        else:
            await send(interaction, "`명령어 인수, 혹은 명령어가 잘못되었습니다. (Help to !config help)`")
            return

    elif command == "help" or command == None:
        msg = """
```
!config summary True : 요약 기능 활성화
!config summary False : 요약 기능 비활성화
!config user <user_id> <real_name> : 유저 ID 매핑 추가/업데이트
!config help : 이 도움말 메시지 표시
```
        """
        await send(interaction, msg)
        return
    await send(interaction, "`명령어 인수, 혹은 명령어가 잘못되었습니다. (Help to !config help)`")
    return

async def summary(interaction: discord.Interaction, 
                  date:str, 
                  flag:int,
                  find:str = None):
    if (stopflag == 1):
        await send(interaction, "API 요청 과부하로, 잠시 서비스를 중지합니다.")
        return
    start_time = time.time()
    log_file = os.path.join('logs', f"{date}.txt")
    if not os.path.exists(log_file):
        await send(interaction, "파일이 존재하지 않거나, 형식이 잘못되었습니다. 날짜 형식: YYYY-MM-DD")
        return
    await loading(interaction)
    notation = await progress_send(interaction, f"`{nowmodel}을 이용해 요약 중...`")
    try:
        pattern = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (.+?): (.+)")
        messages = []

        with open(log_file, 'r', encoding='utf-8') as f:
            for line in f:
                match = pattern.match(line)
                if match:
                    timestamp = match.group(1)
                    user_id = match.group(2)
                    message = match.group(3)
                    dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                    time_formatted = dt.strftime("%H:%M")
                    real_name = USER_MAP.get(user_id, user_id)
                    messages.append(f"[{time_formatted}] {real_name}: {message}")
        await progress_edit(notation, f"`{log_file} 열기 성공. 잠시 기다려주세요.`")
        
        if not messages:
            await loading(interaction, "파일에 분석할 내용이 없습니다.")
            return
        
        combined_text = "\n".join(messages)
        chunk_size = 4000
        chunks = [combined_text[i:i + chunk_size] for i in range(0, len(combined_text), chunk_size)]
        
        await progress_edit(notation, f"`{date}의 총 대화 글자 수: {len(combined_text)}자, {len(chunks)}회 나눠서 분석 시작합니다.`")

        all_summaries = []

        for idx, chunk in enumerate(chunks):
            if flag == 0:
                prompt = f"""
다음은 Discord 채팅방에서의 대화 로그 일부분이다.
총 {len(chunks)}개의 로그 중, {idx+1}번째 로그이다.
해당 내용을 요약해서 전반적인 대화 흐름 및 주제, 자주 나오는 키워드,
나눴던 대화내용(대표적인 발화) 등을 {4000/len(chunks)}자 이내로 정리 및 요약하라.
{chunk}
요약: 
"""
            elif flag == 1:
                prompt = f"""
다음은 Discord 채팅방에서의 대화 로그 일부분이다.
총 {len(chunks)}개의 로그 중, {idx+1}번째 로그이다.
사용자는 다음과 같은 내용을 찾기를 원하고 있다.
사용자가 질의한 내용: {find}
전체 내용 중, 사용자가 질의한 내용과 관련된 대화가 있다면
해당 내용의 대화 흐름 및 주제, 자주 나오는 키워드,
나눴던 대화 내용(대표적인 발화) 등을 {4000/len(chunks)}자 이내로 정리 및 요약하라.
답변은 절대 {4000/len(chunks)}자를 초과해선 안 된다.
만일 해당 내용을 찾을 수 없다면, "(내용없음)" 이라고만 답하라.
{chunk}
요약: 
"""
            try:
                response = await generate_content_timeout(prompt, task_type="summary_map" if flag == 0 else "search_map")
            except LLMError as error:
                await progress_edit(notation, f"`{idx + 1}/{len(chunks)} 청크 처리 실패: {error}`")
                # Do not present incomplete coverage as a complete summary.
                await loading(interaction, "일부 대화를 처리하지 못해 요약을 완료하지 못했습니다. 잠시 후 다시 시도해주세요.")
                return
            summary = response.text if response.text is not None else f"{idx + 1}번째 요약 실패."
            all_summaries.append(summary)
            await progress_edit(notation, f"`{idx + 1}/{len(chunks)} 청크 요약 완료.`")
         # 최종 요약 요청
        await progress_edit(notation, f"`최종 요약 진행 중...`")
        combined_summaries = " ".join(all_summaries)
        if flag == 0:
            final_prompt = f"""
다음은 Discord 대화 로그를 나눠 요약한 부분 요약들입니다. 
이 부분 요약들을 종합하여 전반적인 대화 흐름 및 주제, 자주 나오는 키워드,
나눴던 대화 내용(대표적인 발화) 등을 하나로 통합해서 최종 요약을 1500자 이내로 정리 및 요약하라.
답변은 절대 2000자를 초과해선 안 된다.
줄바꿈 혹은 마크다운 형식을 이용해 보기 편하게 정리하라.
{combined_summaries}

최종 요약:
"""
        elif flag == 1:
            final_prompt = f"""
다음은 Discord 채팅방에서
다음과 같은 내용을 찾아 요약한 부분 요약들이다.
사용자가 질의한 내용: {find}
이 부분 요약들을 종합하여 사용자가 질의한 내용과 관련된 대화가 있다면,
해당 내용과 관련한 전반적인 대화 흐름 및 주제, 자주 나오는 키워드,
나눴던 대화 내용(대표적인 발화) 등을
하나로 통합해서 최종 요약을 1200자 이내로 정리 및 요약하라.
답변은 절대 1500자를 초과해선 안 된다.
줄바꿈 혹은 마크다운 형식을 이용해 보기 편하게 정리하라.
{combined_summaries}

만일 해당 내용을 찾을 수 없다면, 
"{date}에는 해당 내용으로 대화한 기록이 없어요!"
라고만 답하라.

최종 요약:
"""
        final_response = await generate_content_timeout(final_prompt, task_type="summary_reduce" if flag == 0 else "search_reduce")
        final_summary = final_response.text if final_response.text is not None else "최종 요약 실패."
        end_time = time.time()
        elapsed_time = end_time - start_time
        await progress_edit(notation, f"`{nowmodel}: 요약 소요 시간: {elapsed_time:.2f}s`")
        if flag == 0:
            await loading(interaction, f"# {date}에는 이런 대화들을 나눴어요!\n{final_summary}")
        elif flag == 1:
            await loading(interaction, f"# {date}에 나눈 대화 중 `{find}`에 대한 검색 결과입니다.\n{final_summary}")

    except Exception as e:
        await loading(interaction, f"요약 중 오류 발생: {str(e)}")


@app_commands.command(name="요약", description="요약 `YYYY-MM-DD`로 해당 날짜 대화 로그를 분석해 요약해줍니다.")
@app_commands.describe(
    date="날짜 형식은 반드시 YYYY-MM-DD여야합니다."
)
async def 요약(interaction: discord.Interaction, date: str):
    await summary(interaction, date, 0)




@app_commands.command(name="찾기", description="찾기 `YYYY-MM-DD` `찾을 내용`")
@app_commands.describe(
    date="날짜 형식은 반드시 YYYY-MM-DD여야합니다.",
    find="검색어를 입력하세요."
)
async def 찾기(interaction: discord.Interaction, date: str, *,find: str):
    await summary(interaction, date, 1, find)


    
    
@app_commands.command(name="정보", description="봇 정보를 알려줍니다.")
async def 정보(interaction: discord.Interaction):
    now = datetime.fromtimestamp(time.time()).strftime("%Y.%m.%d %H:%M:%S")
    await send(interaction, INFORMATION)
    
@app_commands.command(name="후앰아이", description="sex")
async def 후앰아이(interaction: discord.Interaction):
    await send(interaction, WHO_AM_I)
    t = "[DEBUG] 후앰아이 호출"
    print(t)
    #save__logs("Console", t)   
    
@app_commands.command(name="stop", description="대화 맥락을 강제로 중지합니다.")
async def stop(interaction: discord.Interaction):
    await clear_context("Interrupted")
    await send(interaction, "`대화 맥락이 초기화되었습니다.`")   
    

@app_commands.command(name="질문", description="멍청한 최씨가 답변을 진행합니다.")
@app_commands.describe(
    prompt="최씨에게 하고 싶은 말이 있나요?"
)
async def 질문(interaction: discord.Interaction, *, prompt:str):
    try: 
        await loading(interaction)
        save__logs("USER", prompt)
        response = await generate_content_timeout(f"""
{COMMAND_PERSONA}

[이번 작업]
최씨가 사용자의 질문에 직접 답한다.
가벼운 잡담성 질문이면 최씨답게 짧고 편하게 답한다. 매번 장문의 해설을 붙이지 않는다.
사실·계산·방법을 묻는 질문이면 장난을 치더라도 핵심 답을 빠뜨리지 않는다.
모르는 내용은 아는 척하지 말고 모른다고 한다.

질문: {prompt}
""", task_type="question")
        reply_text = "응애! 대답할 수 없음!"
        if response.text is not None: reply_text = f"Q. {prompt}\nA. {response.text}"
        await send(interaction, reply_text)
        save__logs("최씨 봇", reply_text)
        console_log = f"[DEBUG] 명령어 답변 생성됨. 질의: {prompt} 내용: {reply_text}"
        print(console_log)
        #save__logs("Console", console_log)
    except Exception as e:
        await send(interaction, f"잉! 잘못된 명령 발생! {str(e)}")

@app_commands.command(name="알려줘", description=f"조금 더 똑똑한 최씨가 {nowmodel}을 사용해 답변합니다.")
@app_commands.describe(
    prompt=f"질의에 대한 응답은 {nowmodel}이 담당합니다."
)
async def 알려줘(interaction: discord.Interaction, *, prompt: str):
    try: 
        start_time = time.time()
        save__logs("USER", prompt)
        await loading(interaction)
        start = await progress_send(interaction, f"`{nowmodel} 에서 답변 생성중입니다. 잠시 기다려주세요...`")
        response = await generate_content_timeout(f"""
{COMMAND_PERSONA}

[이번 작업]
최씨가 사용자에게 정보를 알려준다. 정확한 정보 제공이 목적이다.
딱딱한 AI 비서의 설명문 대신, 최씨가 직접 알려주는 말투로 쓴다.
장난스러운 수식어를 실제 정보와 섞어서 사실처럼 말하지 않는다.
단답형으로 굳이 줄이지 않아도 되고 적당한 길이로 설명한다. 너무 긴 정보는 핵심만 2줄까지 요약한다.

정보를 요청하는 질문: {prompt}
""", task_type="info")
        reply_text = "응애! 대답할 수 없음!"
        if response.text is not None: reply_text = f"Q. {prompt}\nA. {response.text}"
        await send(interaction, reply_text)
        end_time = time.time()
        elapsed_time = end_time - start_time
        await progress_edit(start, f"`{nowmodel}에서 답변 생성됨. 경과 시간: {elapsed_time:.2f}s`")
        save__logs("최씨 봇", reply_text)
        console_log = f"[DEBUG] 정보 제공 답변 생성됨. 질의: {prompt} 내용: {reply_text}"
        print(console_log)
        #save__logs("Console", console_log)

    except Exception as e:
        await send(interaction, f"잉! 잘못된 명령 발생! {str(e)}")

@app_commands.command(name="자세히", description=f"매우 똑똑한 최씨가 답변해줍니다. {nowmodel}을 사용해서 말이죠...")
@app_commands.describe(
    prompt=f"질문에 대해 {nowmodel}이 제공하는 아주 상세한 답변을 받을 수 있습니다."
)
async def 자세히(interaction: discord.Interaction, *, prompt: str):
    try: 
        start_time = time.time()
        save__logs("USER", prompt)
        await loading(interaction)
        start = await progress_send(interaction, f"`{nowmodel} 에서 답변 생성중입니다. 잠시 기다려주세요...`")
        response = await generate_content_timeout(f"""
{COMMAND_PERSONA}

[이번 작업]
최씨가 직접 자세히 설명한다.
단어를 물으면 그 단어를 자세히 설명하고, 문장이면 그 내용을 그대로 다룬다.
설명의 충분성과 이해 가능성을 최우선으로 한다. 여기서는 1~2줄 제한을 적용하지 않는다.
음슴체나 특정 유행어로 문장을 고정하지 않는다. 설명에 맞는 자연스러운 문장으로 이어서 말한다.
농담과 감탄사는 설명을 방해하지 않는 선에서만 섞는다. 이모티콘은 사용하지 않는다.
잘 모르거나 출처가 불분명한 정보라면 모르겠다고 한다.
출력 제한: 2000자 이내로 답변한다.

정보를 요청하는 질문: {prompt}
""", task_type="detail")
        reply_text = "응애! 대답할 수 없음!"
        if response.text is not None: reply_text = f"Q. {prompt}\nA. {response.text}"
        await send(interaction, reply_text)
        end_time = time.time()
        elapsed_time = end_time - start_time
        await progress_edit(start, f"`{nowmodel}에서 답변 생성됨. 경과 시간: {elapsed_time:.2f}s`")
        save__logs("최씨 봇", reply_text)
        console_log = f"[DEBUG] 자세한 답변 생성됨. 질의: {prompt} 내용: {reply_text}"
        print(console_log)
        #save__logs("Console", console_log)
    except Exception as e:
        await send(interaction, f"잉! 잘못된 명령 발생! {str(e)}")

@app_commands.command(name="패치노트", description=f"{BUILD_VERSION}의 최신 패치노트를 확인하세요!")
async def 패치노트(interaction: discord.Interaction):
    await send(interaction, PATCHNOTE)
    t = "[DEBUG] 패치노트 호출"
    print(t)
    #save__logs("Console", t)

@app_commands.command(name="언제와", description="최씨가 언제 떠났을까요?")
async def 언제와(interaction: discord.Interaction):
    e_time = time_since(DEP_TIME)
    r_time = time_since(RET_TIME)
    t = f"최씨가 우리를 최초로 유기한 날로부터 {e_time} 지났습니다...."
    t2 = f"최씨가 염치없게 돌아온 날로부터 {r_time} 지났습니다...."
    await send(interaction, f"{t}\n{t2}")
    print(t)
    #save__logs("Console", t)

@app_commands.command(name="유저", description="유저 이름 매핑 확인이 가능합니다.")
@app_commands.describe(
    option="미입력: 매핑 출력"
)
async def 유저(interaction: discord.Interaction, option: str = None, user_name: str = None):
    if option is None:
        await send(interaction, f"```{USER_MAP}```")
    else:
        await send(interaction, "`잘못된 옵션입니다. !유저 help 명령어로 도움말을 확인하세요.`")
    if option and option.lower() == "help":
        help_msg = """
        ```
추가 예정입니다.
```
        """



async def menu_recommand(interaction: discord.Interaction, time, message: str = None):
    if message is None:
        message = "없음"

    if message == "help":
        await send(interaction, f"{time} 메뉴 추천을 위한 명령어입니다. 사용법: `!점메추 <추천 요청사항>`")
   
    await loading(interaction)
    notation = await progress_send(interaction, f"`{nowmodel}이 최적의 {time} 메뉴를 추천합니다...`")
    
    try:
        response = await generate_content_timeout(f"""
너는 '무난하고 현실적인 {time} 메뉴'를 추천하는 AI야.

기본 원칙은 다음과 같아:
1. **일상적으로 먹을 수 있는 현실적인 메뉴만 추천**해. 지나치게 특이하거나 퓨전 성격이 강한 메뉴(예: 김치볶음밥 그라탕, 명란 아보카도 볶음밥 등)는 제외해.
2. **추천되는 메뉴는 한식, 중식, 일식, 양식, 분식 등에서 고르게 분포**되도록 해. 항상 특정 한두 메뉴만 반복하지 말고, **메뉴 풀이 넓고 다양하게 유지**해.
3. **매번 무작위(random)**로 메뉴를 구성해.
4. 추천 메뉴는 **식당, 매점, 편의점, 도시락 가게, 배달 앱, 슈퍼마켓, 대형마트 등에서 실제로 구매 가능한 메뉴여야 해.**
5. 사용자가 아래에 제시한 요청사항이 있다면, 이를 **최우선으로 반영**하고 그렇지 않으면 **무난한 추천**으로 구성해.

요청사항: {message}

총 15개의 메뉴를 추천하고,
형식은 아래처럼 작성해. 불필요한 표현은 넣지 마.
**메뉴 추천**
1. 후보군1: 설명
2. 후보군2: 설명
3. 후보군3: 설명
(...)
10. 후보군15: 설명
""", task_type="menu_candidates")
        reply_text = "응애! 대답할 수 없음!"
        if response.text is not None: reply_text = response.text
        print(f"[DEBUG] {reply_text}")
        final_reply = await generate_content_timeout(f"""
{VOICE_ONLY}

[이번 작업]
최씨가 {time} 메뉴를 골라서 추천한다.
    {reply_text}
    상기 15개의 메뉴 추천 후보군 중 5개만 완전 무작위로 고르되,
    다음 요청사항이 있다면 최우선적으로 반영하여 골라.
    요청사항: {message}
    메뉴명은 그대로 두고, 각 설명만 최씨 말투로 짧게 쓴다. 00100이나 (마이크 끄는 소리)는 출력하지 않는다.
    형식은 아래처럼 작성해. 불필요한 표현은 넣지 마.
    **{time}메뉴 추천**
    1. 메뉴명: 설명
    2. 메뉴명: 설명
    3. 메뉴명: 설명     
    4. 메뉴명: 설명 
    5. 메뉴명: 설명                                      
    """, task_type="menu_select")
        final_reply = final_reply.text if final_reply.text is not None else "응애! 대답할 수 없음!"
        # The original progress message is now the final answer: retain it.
        await loading(interaction, final_reply)
        save__logs("최씨 봇", final_reply)
    except Exception as e:
        await send(interaction, f"잉! 잘못된 명령 발생! {str(e)}")


@app_commands.command(name="점메추", description="점심 메뉴가 고민이신가요? 최씨가 추천해드립니다!")
@app_commands.describe(
    message="요청사항이 있으시면 추가로 적어주세요."
)
async def 점메추(interaction: discord.Interaction, *, message: str = None):
    await menu_recommand(interaction, "점심", message)


@app_commands.command(name="저메추", description="저녁 메뉴가 고민이신가요? 최씨가 추천해드립니다!")
@app_commands.describe(
    message="요청사항이 있으시면 추가로 적어주세요."
)
async def 저메추(interaction: discord.Interaction, *, message: str = None):
    await menu_recommand(interaction, "저녁", message)



class TranslateModal(Modal, title="번역할 문장을 입력하세요."):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.message_input = TextInput(
            label="번역할 문장",
            placeholder="번역할 문장을 입력하세요.",
            style=discord.TextStyle.paragraph,
            max_length=500
        )
        self.add_item(self.message_input)
    async def on_submit(self, interaction: discord.Interaction):
        await self.callback(interaction, self.message_input.value)

class TranslateView(View):
    def __init__(self):
        super().__init__(timeout=300)
        self.message = None
        self.target_lang = None
        
        self.select = Select(
            placeholder="번역할 언어를 선택하세요",
            options=[
                discord.SelectOption(label="한국어", value="한국어"),
                discord.SelectOption(label="영어", value="영어"),
                discord.SelectOption(label="중국어", value="중국어"),
                discord.SelectOption(label="일본어", value="일본어"),
            ]
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

        self.input_button = Button(label="문장 입력", style=discord.ButtonStyle.secondary)
        self.input_button.callback = self.input_callback
        self.add_item(self.input_button)

        self.translate_button = Button(label="번역!", style=discord.ButtonStyle.success)
        self.translate_button.callback = self.translate_callback
        self.add_item(self.translate_button)

    async def input_callback(self, interaction: discord.Interaction):
        modal = TranslateModal(self.set_message)
        await interaction.response.send_modal(modal)

    async def set_message(self, interaction: discord.Interaction, message: str):
        self.message = message
        await loading(interaction, thinking=False)
        # await send(interaction, f"문장 저장됨: {message}", ephemeral=True)

    async def select_callback(self, interaction: discord.Interaction):
        self.target_lang = self.select.values[0]
        await loading(interaction, thinking=False)
        # await send(interaction, f"언어 선택됨 {self.select.values[0]}", ephemeral=True)

    async def translate_callback(self, interaction: discord.Interaction):
        if not self.message:
            await send(interaction, "문장을 입력해주세요!", ephemeral=True)
            return
        if not self.target_lang:
            await send(interaction, "언어를 선택해주세요!", ephemeral=True)
            return
        
        source_text, target_lang = self.message, self.target_lang
        prompt = f"""
너는 번역기고, 이제부터 내가 준 문장에 대해 번역만을 출력해야돼.
다음 문장의 언어가 무엇인지 판별하고, 해당 문장을 {target_lang}로 자연스럽게 번역해줘.
번역할 문장: {source_text}
다음 조건을 준수해.
1. 언어 감지는 확실하게 하며, 언어가 감지되지 않거나 불확실하면 "언어 감지 실패!"만 출력
2. 감지 언어와 번역 언어가 같으면 그냥 출력해.
3. 목표 언어가 중국어라면, 번역된 문장 뒤에 한어병음을 괄호에 넣어 표기해줘.
4. 목표 언어가 일본어라면, 문장 뒤에 히라가나로만 된 문장을 추가로 괄호에 넣어 표기해줘.
5. 위 4개 상황이 아니라면, 자연스럽게 {target_lang}로 번역된 문장만을 출력해.
"""
        await loading(interaction)

        try:
            response = await generate_content_timeout(prompt, task_type="translation")
            result = response.text if response.text is not None else "번역 실패!"
            await loading(interaction, f"**원본 언어**: {source_text}\n**`{target_lang}`번역**: {result}")
        except Exception as e:
            await loading(interaction, f"칩임자 발견, 자가방어시스템을 가동합니다. {str(e)}")

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if hasattr(self, 'original_message'):
            try:
                await self.original_message.edit(content="`번역 세션 만료됨.`", view=self)
            except Exception as e:
                print(f"세션 종료 실패! {e}")

@app_commands.command(name="번역", description="번역 기능입니다.")
async def 번역(interaction: discord.Interaction):
    view = TranslateView()
    await send(interaction, "최씨 번역기입니다. \n언어 선택 후 문장 입력을 눌러 번역할 문장을 입력해주세요. \n그 후, 번역 버튼을 누르면 번역이 진행됩니다.", view=view)
    view.original_message = await interaction.original_response()




@app_commands.command(name="알림", description="역할 부여를 통해 특정 알림을 받을 수 있습니다.")
@app_commands.describe(role="알림을 받을 역할")
async def 알림(interaction: discord.Interaction, role: str):
    member = interaction.user
    guild = interaction.guild
    role_obj = guild.get_role(int(role))

    if not role_obj:
        await send(interaction, "해당 역할이 존재하지 않습니다.", ephemeral=True)
        return
    try:
        await member.add_roles(role_obj)
        await send(interaction, f"`{role_obj.name}` 역할이 부여되었습니다. 이 역할을 통해 알림을 받을 수 있습니다.", ephemeral=True)
    except Exception as e:
        await send(interaction, f"역할 부여에 실패했습니다: {e}", ephemeral=True)


@app_commands.command(name="해제", description="역할 부여를 통해 특정 알림을 해제할 수 있습니다.")
@app_commands.describe(role="알림을 해제할 역할")
async def 해제(interaction: discord.Interaction, role: str):
    member = interaction.user
    guild = interaction.guild
    role_obj = guild.get_role(int(role))

    if not role_obj:
        await send(interaction, "해당 역할이 존재하지 않습니다.", ephemeral=True)
        return
    try:
        await member.remove_roles(role_obj)
        await send(interaction, f"`{role_obj.name}` 역할이 해제되었습니다. 이 역할을 통해 알림을 받을 수 없습니다.", ephemeral=True)
    except Exception as e:
        await send(interaction, f"역할 해제에 실패했습니다: {e}", ephemeral=True)


@app_commands.command(name="공지", description="공지 채널에 공지글을 올리고, 해당 글에 스레드를 자동으로 엽니다.")
@app_commands.describe(
    title="공지 제목",
    content="공지 내용"
)
async def 공지(interaction: discord.Interaction, title: str, *, content: str):
    await interaction.response.defer(ephemeral=True)
    auther_mention = interaction.user.mention

    channel = client.get_channel(1464234958347436133)
    if channel is None:
        try: 
            channel = await client.fetch_channel(1464234958347436133)
        except Exception as e:
            await send(interaction, f"공지 채널을 찾을 수 없습니다: {e}", ephemeral=True)
            return
    if not isinstance(channel, discord.TextChannel):
        await send(interaction, "공지 채널이 텍스트 채널이 아닙니다.", ephemeral=True)
        return

    allowed = discord.AllowedMentions(roles=True, users=True, everyone=True)

    try:
        msg = await channel.send(f"# **{title}**\nby: {auther_mention}\n{content}", allowed_mentions=allowed)
    except Exception as e:
        await send(interaction, f"공지글 작성에 실패했습니다: {e}", ephemeral=True)
        return
    
    thread_name = re.sub(r"\s+", " ", title.strip().replace("\n", " "))
    thread_name = thread_name[:100] if thread_name else "공지"

    try:
        thread = await msg.create_thread(
            name=thread_name,
            auto_archive_duration=1440,  # 24시간 (60/1440/4320/10080 중 선택)
            reason="공지 스레드 자동 생성"
        )
        # 스레드 첫 안내(원하면 문구 변경/삭제)
        await thread.send("질문/댓글은 이 스레드에 남겨주세요.", allowed_mentions=allowed)
    except Exception as e:
        await interaction.followup.send(
            f"[WARN] 공지는 작성했지만 스레드 생성 실패: {e}\n공지 링크: {msg.jump_url}",
            ephemeral=True
        )
        return

    await interaction.followup.send(
        f"공지 등록 완료.\n공지: {msg.jump_url}\n스레드: {thread.mention}",
        ephemeral=True
    )



@알림.autocomplete("role")
async def 알림_autocomplete(interaction: discord.Interaction, current: str):
    roles = [
        r for r in interaction.guild.roles
        if r.id in ROLE_WHITELIST and current.lower() in r.name.lower()
    ]
    return [app_commands.Choice(name=r.name, value=str(r.id)) for r in roles][:25]


@해제.autocomplete("role")
async def 해제_autocomplete(interaction: discord.Interaction, current: str):
    roles = [
        r for r in interaction.guild.roles
        if r.id in ROLE_WHITELIST and current.lower() in r.name.lower()
    ]
    return [app_commands.Choice(name=r.name, value=str(r.id)) for r in roles][:25]




conversation = ConversationQueue(process_conversation_message)


class ChoiClient(discord.Client):
    async def close(self):
        for loop in (send_announcement, check_context, send_waist):
            loop.cancel()
        await conversation.aclose()
        try:
            if llm_router is not None:
                await llm_router.aclose()
        finally:
            await super().close()


def initialize_runtime(settings: Settings, *, router=None, client_factory=None, tree_factory=None):
    """Prepare one process runtime without connecting; dependencies may be fakes.

    Global conversation state intentionally retains the existing sharing scope.
    Call before handling events; this is not a multi-instance application factory.
    """
    global API_KEYS, client, tree, llm_router
    validate_settings(settings)
    if router is None:
        from bot.llm.gemini import GeminiAdapter
        router = LLMRouter(
            {"gemini": GeminiAdapter(settings.api_keys, MODEL, key_ids=settings.key_ids or None)}, task_policies(MODEL),
        )
    intents = discord.Intents.default()
    intents.messages = True
    intents.message_content = True
    intents.members = True
    intents.presences = True
    intents.guilds = True
    new_client = (client_factory or ChoiClient)(intents=intents)
    new_tree = (tree_factory or app_commands.CommandTree)(new_client)
    new_client.event(on_ready)
    new_client.event(on_message)
    new_tree.error(on_application_command_error)
    for command in globals().values():
        if isinstance(command, app_commands.Command):
            new_tree.add_command(command)
    os.makedirs(LOG_FOLDER, exist_ok=True)
    API_KEYS = settings.api_keys
    llm_router = router
    client, tree = new_client, new_tree
    return client


def main():
    settings = load_settings()
    runtime_client = initialize_runtime(settings)
    runtime_client.run(settings.discord_token)


if __name__ == "__main__":
    main()
