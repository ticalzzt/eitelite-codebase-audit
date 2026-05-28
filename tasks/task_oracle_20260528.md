# Task: Oracle VPS nginx 安全加固
## 分配给：tico-oracle (163.192.17.78)
## 优先级：P1
## 路径：/home/ubuntu/eitelite/

你有 sudo 权限（已配 sudoers）和 git push 权限（指向 eitelite-codebase-audit）。
完成后必须 git commit + git push + 验证。

---

## Task 1：加 nginx 安全头

**修法**：编辑 nginx 配置，加安全头

```bash
# 找到 nginx 配置
ls /etc/nginx/sites-enabled/
# 在 server 块的 listen 443 ssl; 后加：
#     add_header X-Content-Type-Options "nosniff" always;
#     add_header X-Frame-Options "DENY" always;
#     add_header Referrer-Policy "strict-origin-when-cross-origin" always;
#     add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
```

**验证**：
```bash
curl -s -D - https://ORACLE_IP/ | grep -iE 'x-content|x-frame|csp|hsts'
sudo nginx -t
sudo nginx -s reload
```

---

## Task 2：加 favicon + robots.txt

在 nginx root 目录或 static 目录放 favicon.ico 和 robots.txt

---

## 完成条件
- [ ] nginx 安全头已加并验证
- [ ] git commit + git push
