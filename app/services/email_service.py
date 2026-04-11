"""邮件发送服务"""
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
import json

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.models import SystemSetting
from app.logging_config import get_logger

logger = get_logger("lmaicloud.email")


class EmailConfig:
    """邮件配置类 - 支持从配置文件或数据库读取"""
    def __init__(
        self,
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_user: str = "",
        smtp_password: str = "",
        from_email: str = "",
        from_name: str = "龙虾云",
        use_tls: bool = True,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.from_email = from_email
        self.from_name = from_name
        self.use_tls = use_tls
    
    @property
    def is_configured(self) -> bool:
        """检查邮件服务是否已配置"""
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)


async def get_email_config(db: Optional[AsyncSession] = None) -> EmailConfig:
    """
    获取邮件配置
    优先从数据库读取，如果数据库没有配置则使用配置文件
    """
    config = EmailConfig(
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        smtp_user=settings.smtp_user,
        smtp_password=settings.smtp_password,
        from_email=settings.smtp_from_email,
        from_name=settings.smtp_from_name,
        use_tls=settings.smtp_use_tls,
    )
    
    # 尝试从数据库获取动态配置
    if db:
        try:
            result = await db.execute(
                select(SystemSetting).where(
                    SystemSetting.key.in_([
                        "smtp_host", "smtp_port", "smtp_user", "smtp_password",
                        "smtp_from_email", "smtp_from_name", "smtp_use_tls"
                    ])
                )
            )
            db_settings = {s.key: json.loads(s.value) for s in result.scalars().all()}
            
            # 用数据库配置覆盖（如果存在且非空）
            if db_settings.get("smtp_host"):
                config.smtp_host = db_settings["smtp_host"]
            if db_settings.get("smtp_port"):
                config.smtp_port = db_settings["smtp_port"]
            if db_settings.get("smtp_user"):
                config.smtp_user = db_settings["smtp_user"]
            if db_settings.get("smtp_password"):
                config.smtp_password = db_settings["smtp_password"]
            if db_settings.get("smtp_from_email"):
                config.from_email = db_settings["smtp_from_email"]
            if db_settings.get("smtp_from_name"):
                config.from_name = db_settings["smtp_from_name"]
            if "smtp_use_tls" in db_settings:
                config.use_tls = db_settings["smtp_use_tls"]
        except Exception as e:
            logger.warning(f"从数据库读取邮件配置失败: {e}")
    
    return config


def send_email_sync(
    config: EmailConfig,
    to_email: str,
    subject: str,
    html_content: str,
    text_content: Optional[str] = None
) -> tuple[bool, Optional[str]]:
    """
    同步发送邮件
    返回 (是否成功, 错误信息)
    
    根据端口自动选择加密方式：
    - 端口465: 使用SSL (SMTP_SSL)
    - 端口587/25: 使用STARTTLS (如果use_tls为True)
    """
    if not config.is_configured:
        logger.warning("邮件服务未配置，跳过发送")
        return False, "邮件服务未配置"
    
    # 记录配置信息（不含密码）
    logger.info(f"邮件发送配置: host={config.smtp_host}, port={config.smtp_port}, user={config.smtp_user}, use_tls={config.use_tls}")
    
    try:
        # 创建邮件
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = f"{config.from_name} <{config.from_email}>"
        message["To"] = to_email
        
        # 添加纯文本版本
        if text_content:
            part1 = MIMEText(text_content, "plain", "utf-8")
            message.attach(part1)
        
        # 添加HTML版本
        part2 = MIMEText(html_content, "html", "utf-8")
        message.attach(part2)
        
        context = ssl.create_default_context()
        
        # 根据端口选择连接方式
        if config.smtp_port in (465, 994):
            # SSL模式 (端口465/994) - 一开始就使用SSL连接
            logger.info(f"使用SSL模式连接 {config.smtp_host}:{config.smtp_port}")
            with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30, context=context) as server:
                server.login(config.smtp_user, config.smtp_password)
                server.sendmail(config.from_email, to_email, message.as_string())
        elif config.use_tls:
            # STARTTLS模式 (端口587等) - 先建立普通连接再升级到TLS
            logger.info(f"使用STARTTLS模式连接 {config.smtp_host}:{config.smtp_port}")
            with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as server:
                server.starttls(context=context)
                server.login(config.smtp_user, config.smtp_password)
                server.sendmail(config.from_email, to_email, message.as_string())
        else:
            # 无加密模式
            logger.info(f"使用无加密模式连接 {config.smtp_host}:{config.smtp_port}")
            with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as server:
                server.login(config.smtp_user, config.smtp_password)
                server.sendmail(config.from_email, to_email, message.as_string())
        
        logger.info(f"邮件发送成功: {to_email}")
        return True, None
    
    except smtplib.SMTPAuthenticationError as e:
        error_msg = f"SMTP认证失败: 用户名或密码错误 ({e.smtp_code})"
        logger.error(f"邮件发送失败: {to_email}, {error_msg}")
        return False, error_msg
    except smtplib.SMTPConnectError as e:
        error_msg = f"SMTP连接失败: 无法连接到服务器 {config.smtp_host}:{config.smtp_port}"
        logger.error(f"邮件发送失败: {to_email}, {error_msg}")
        return False, error_msg
    except smtplib.SMTPServerDisconnected as e:
        error_msg = f"SMTP服务器断开连接: {str(e)}"
        logger.error(f"邮件发送失败: {to_email}, {error_msg}")
        return False, error_msg
    except smtplib.SMTPRecipientsRefused as e:
        error_msg = f"收件人被拒绝: {to_email}"
        logger.error(f"邮件发送失败: {to_email}, {error_msg}")
        return False, error_msg
    except smtplib.SMTPSenderRefused as e:
        error_msg = f"发件人被拒绝: {config.from_email}"
        logger.error(f"邮件发送失败: {to_email}, {error_msg}")
        return False, error_msg
    except TimeoutError:
        error_msg = f"连接超时: 无法连接到 {config.smtp_host}:{config.smtp_port}"
        logger.error(f"邮件发送失败: {to_email}, {error_msg}")
        return False, error_msg
    except OSError as e:
        error_msg = f"网络错误: {str(e)}"
        logger.error(f"邮件发送失败: {to_email}, {error_msg}")
        return False, error_msg
    except Exception as e:
        error_msg = f"发送失败: {str(e)}"
        logger.error(f"邮件发送失败: {to_email}, {error_msg}")
        return False, error_msg


def generate_activation_email_html(
    user_email: str,
    activation_link: str,
    site_name: str = "龙虾云",
    expire_hours: int = 24
) -> tuple[str, str]:
    """
    生成激活邮件的HTML和纯文本内容
    返回 (html_content, text_content)
    """
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 0; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f5f5f5;">
    <div style="max-width: 600px; margin: 0 auto; padding: 40px 20px;">
        <div style="background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1); overflow: hidden;">
            <!-- Header -->
            <div style="background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%); padding: 30px; text-align: center;">
                <h1 style="color: #ffffff; margin: 0; font-size: 28px;">{site_name}</h1>
                <p style="color: rgba(255,255,255,0.9); margin: 10px 0 0 0; font-size: 14px;">大模型AI算力云平台</p>
            </div>
            
            <!-- Content -->
            <div style="padding: 40px 30px;">
                <h2 style="color: #1f2937; margin: 0 0 20px 0; font-size: 22px;">欢迎注册 {site_name}！</h2>
                <p style="color: #4b5563; line-height: 1.6; margin: 0 0 20px 0;">
                    您好，感谢您注册 {site_name} 账户。请点击下方按钮激活您的邮箱：
                </p>
                
                <!-- Activation Button -->
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{activation_link}" 
                       style="display: inline-block; background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%); 
                              color: #ffffff; text-decoration: none; padding: 14px 40px; border-radius: 8px; 
                              font-size: 16px; font-weight: 600; box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3);">
                        激活邮箱
                    </a>
                </div>
                
                <p style="color: #6b7280; font-size: 14px; line-height: 1.6; margin: 20px 0;">
                    如果按钮无法点击，请复制以下链接到浏览器打开：
                </p>
                <p style="color: #6366f1; font-size: 13px; word-break: break-all; background-color: #f3f4f6; 
                          padding: 12px; border-radius: 6px; margin: 0 0 20px 0;">
                    {activation_link}
                </p>
                
                <div style="border-top: 1px solid #e5e7eb; padding-top: 20px; margin-top: 30px;">
                    <p style="color: #9ca3af; font-size: 13px; margin: 0;">
                        ⏰ 此链接将在 {expire_hours} 小时后失效
                    </p>
                    <p style="color: #9ca3af; font-size: 13px; margin: 10px 0 0 0;">
                        🔒 如果您没有注册账户，请忽略此邮件
                    </p>
                </div>
            </div>
            
            <!-- Footer -->
            <div style="background-color: #f9fafb; padding: 20px 30px; text-align: center;">
                <p style="color: #9ca3af; font-size: 12px; margin: 0;">
                    © 2024 {site_name}. All rights reserved.
                </p>
            </div>
        </div>
    </div>
</body>
</html>
"""
    
    text_content = f"""
欢迎注册 {site_name}！

您好，感谢您注册 {site_name} 账户。请点击以下链接激活您的邮箱：

{activation_link}

此链接将在 {expire_hours} 小时后失效。

如果您没有注册账户，请忽略此邮件。

© 2024 {site_name}
"""
    
    return html_content, text_content


async def send_activation_email(
    db: AsyncSession,
    to_email: str,
    activation_token: str,
    site_name: str = "龙虾云",
    expire_hours: int = 24
) -> bool:
    """发送激活邮件"""
    config = await get_email_config(db)
    
    # 构建激活链接
    frontend_url = settings.frontend_url.rstrip('/')
    activation_link = f"{frontend_url}/activate?token={activation_token}"
    
    # 生成邮件内容
    html_content, text_content = generate_activation_email_html(
        user_email=to_email,
        activation_link=activation_link,
        site_name=site_name,
        expire_hours=expire_hours
    )
    
    success, _ = send_email_sync(
        config=config,
        to_email=to_email,
        subject=f"【{site_name}】请激活您的邮箱",
        html_content=html_content,
        text_content=text_content
    )
    return success


def generate_password_reset_email_html(
    activation_link: str,
    site_name: str = "龙虾云",
    expire_minutes: int = 30
) -> tuple[str, str]:
    """生成密码重置邮件的HTML和纯文本内容"""
    html_content = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;background-color:#f5f5f5;">
<div style="max-width:600px;margin:0 auto;padding:40px 20px;">
 <div style="background:#fff;border-radius:12px;box-shadow:0 4px 6px rgba(0,0,0,.1);overflow:hidden;">
  <div style="background:linear-gradient(135deg,#ef4444 0%,#f97316 100%);padding:30px;text-align:center;">
   <h1 style="color:#fff;margin:0;font-size:28px;">{site_name}</h1>
   <p style="color:rgba(255,255,255,.9);margin:10px 0 0;font-size:14px;">密码重置</p>
  </div>
  <div style="padding:40px 30px;">
   <h2 style="color:#1f2937;margin:0 0 20px;font-size:22px;">重置您的密码</h2>
   <p style="color:#4b5563;line-height:1.6;margin:0 0 20px;">
    您好，我们收到了您的密码重置请求。请点击下方按钮设置新密码：
   </p>
   <div style="text-align:center;margin:30px 0;">
    <a href="{activation_link}"
       style="display:inline-block;background:linear-gradient(135deg,#ef4444 0%,#f97316 100%);
              color:#fff;text-decoration:none;padding:14px 40px;border-radius:8px;
              font-size:16px;font-weight:600;box-shadow:0 4px 12px rgba(239,68,68,.3);">
      重置密码
    </a>
   </div>
   <p style="color:#6b7280;font-size:14px;line-height:1.6;margin:20px 0;">
    如果按钮无法点击，请复制以下链接到浏览器打开：
   </p>
   <p style="color:#ef4444;font-size:13px;word-break:break-all;background:#f3f4f6;padding:12px;border-radius:6px;margin:0 0 20px;">
    {activation_link}
   </p>
   <div style="border-top:1px solid #e5e7eb;padding-top:20px;margin-top:30px;">
    <p style="color:#9ca3af;font-size:13px;margin:0;">⏰ 此链接将在 {expire_minutes} 分钟后失效</p>
    <p style="color:#9ca3af;font-size:13px;margin:10px 0 0;">🔒 如果您没有请求重置密码，请忽略此邮件，您的账户仍然安全</p>
   </div>
  </div>
  <div style="background:#f9fafb;padding:20px 30px;text-align:center;">
   <p style="color:#9ca3af;font-size:12px;margin:0;">© 2025 {site_name}. All rights reserved.</p>
  </div>
 </div>
</div>
</body>
</html>
"""
    text_content = f"""重置您的密码

您好，我们收到了您的密码重置请求。请点击以下链接设置新密码：

{activation_link}

此链接将在 {expire_minutes} 分钟后失效。

如果您没有请求重置密码，请忽略此邮件。

© 2025 {site_name}
"""
    return html_content, text_content


async def send_password_reset_email(
    db: AsyncSession,
    to_email: str,
    reset_token: str,
    site_name: str = "龙虾云",
    expire_minutes: int = 30
) -> bool:
    """发送密码重置邮件"""
    config = await get_email_config(db)
    frontend_url = settings.frontend_url.rstrip('/')
    reset_link = f"{frontend_url}/forgot-password?token={reset_token}"
    html_content, text_content = generate_password_reset_email_html(
        activation_link=reset_link, site_name=site_name, expire_minutes=expire_minutes
    )
    success, _ = send_email_sync(
        config=config, to_email=to_email,
        subject=f"【{site_name}】密码重置",
        html_content=html_content, text_content=text_content
    )
    return success


async def send_test_email(db: AsyncSession, to_email: str) -> tuple[bool, Optional[str]]:
    """发送测试邮件，返回 (成功, 错误信息)"""
    config = await get_email_config(db)
    
    html_content = """
    <div style="font-family: Arial, sans-serif; padding: 20px;">
        <h2>邮件服务测试</h2>
        <p>如果您收到这封邮件，说明邮件服务配置正确！</p>
        <p style="color: #666;">— 龙虾云 系统</p>
    </div>
    """
    
    return send_email_sync(
        config=config,
        to_email=to_email,
        subject="【龙虾云】邮件服务测试",
        html_content=html_content,
        text_content="邮件服务测试\n\n如果您收到这封邮件，说明邮件服务配置正确！"
    )
