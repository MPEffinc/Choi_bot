import os
import re

# 처리할 폴더 경로
folder_path = './logs_bak'  # 여기 너 폴더 경로로 바꿔줘

# 정규표현식 패턴: [DEBUG] 이미 초기화됨 이 포함된 줄
pattern = re.compile(r'\[DEBUG\] 이미 초기화됨.*')

# 폴더 안의 모든 txt 파일 순회
for filename in os.listdir(folder_path):
    if filename.endswith('.txt'):
        file_path = os.path.join(folder_path, filename)
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # 패턴에 맞는 줄 제거
        cleaned_lines = [line for line in lines if not pattern.search(line)]
        
        # 파일 덮어쓰기
        with open(file_path, 'w', encoding='utf-8') as f:
            f.writelines(cleaned_lines)

print("모든 파일에서 [DEBUG] 이미 초기화됨 줄 제거 완료")

