# Cookie 目录说明
cookies/
======
把各平台的 **Netscape 格式 cookies.txt** 放这里，程序会自动按平台加载（可选）。

| 文件名 | 用途 |
|---|---|
| douyin.txt | 抖音（提升受限内容成功率；公开视频通常不需要） |
| xiaohongshu.txt | 小红书（多数内容需要登录态） |
| bilibili.txt | B站（大会员/登录可见内容） |
| kuaishou.txt | 快手（受限内容） |
| youtube.txt | YouTube（年龄限制/会员内容） |
| default.txt | 兜底：其它平台统一使用 |

获取方式（推荐）：用项目自带的内置浏览器，**不需要动你自己的浏览器**

```bash
python login_cookie.py xiaohongshu   # 弹出独立窗口，登录后自动写出 cookies/xiaohongshu.txt
python login_cookie.py kuaishou      # 平台可选：xiaohongshu / kuaishou / douyin / bilibili / youtube
```

- 登录态存在 `cookies/_browser_profile/`（独立 profile，不会被 Git 提交）
- 默认用 **Microsoft Edge** 打开；窗口常驻复用，再次运行同一命令**不会新开窗口**
- 只会保留一个平台标签页（自带的「新标签页」会被自动关闭）
- 会话未过期时**无需重新登录**；只想检查现有登录态：加 `--check`（不弹窗）

其它方式：用 "Get cookies.txt" 类扩展导出 → 另存为该平台文件名，放进本目录。

注意事项：
- cookie 等同登录凭证：**不要分享、不要提交到 Git**
- 只在需要时配置；公开内容通常无需 cookie
- 文件为空或不存在时自动跳过，不影响原有流程
