import os
import json

# --- 你需要修改的配置 ---
# 存放表情包图片的本地文件夹路径 (我已经帮你填好了)
image_folder_path = r"C:\Users\<user>\Desktop\AstrBotLauncher-0.1.5.6\表情包"
# 你在GitHub上的用户名 (我已经帮你填好了)
github_username = "JuryBu"
# 你在GitHub上的仓库名 (我已经帮你填好了)
repo_name = "Emojis"
# 生成的JSON文件名
output_json_file = "my_emojis.json"
# --- 修改结束 ---

emojis = []
base_url = f"https://cdn.jsdelivr.net/gh/{github_username}/{repo_name}/"

# 遍历文件夹中的所有文件
# 使用os.walk来确保能处理子文件夹（如果未来有的话）
for root, dirs, files in os.walk(image_folder_path):
    for filename in files:
        if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
            # 从文件名（不含扩展名）获取关键词
            keywords = os.path.splitext(filename)[0]

            # 处理可能存在的子文件夹路径
            relative_path = os.path.relpath(os.path.join(root, filename), image_folder_path)
            # 保证URL路径使用'/'
            url_path = relative_path.replace('\\', '/')

            # 构建完整的图片URL
            image_url = base_url + url_path

            # 添加到列表
            emojis.append({
                "name": keywords,
                "url": image_url
            })

# 将列表写入JSON文件
with open(output_json_file, 'w', encoding='utf-8') as f:
    json.dump(emojis, f, ensure_ascii=False, indent=2)

print(f"成功生成 {output_json_file}，包含 {len(emojis)} 个表情包！")
