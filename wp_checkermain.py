import asyncio
import json
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, TimeoutError
import pyperclip 
import traceback

# --- ГЛОБАЛЬНЫЕ НАСТРОЙКИ ДЛЯ ПЛОХОГО ИНТЕРНЕТА ---
TIMEOUT_GOTO = 60000  # Увеличен таймаут навигации до 60 секунд
TIMEOUT_SELECTOR = 90000 # Увеличен таймаут ожидания селектора до 90 секунд
RETRY_ATTEMPTS = 3     # Количество попыток для критических шагов
RETRY_DELAY = 10       # Задержка между повторными попытками в секунда
# ----------------------------------------------------

class WordPressPluginInstaller:
    MAX_INSTALL_RETRIES = 3
    RETRY_DELAY_SECONDS = 5

    def __init__(self, headless: bool = False):
        self.headless = True 
        self.cwd = Path.cwd()
        self.results_log = []
    
    # --- НОВЫЙ МЕТОД: НАДЕЖНАЯ НАВИГАЦИЯ ---
    async def _reliable_goto(self, page: Page, url: str, attempt: int = 1) -> bool:
        """Навигация с повторными попытками при таймауте."""
        for i in range(attempt):
            try:
                print(f"   [Попытка {i+1}/{attempt}] Переход: {url}")
                await page.goto(
                    url, 
                    # Используем 'domcontentloaded' вместо 'load' или 'networkidle'
                    wait_until='domcontentloaded', 
                    timeout=TIMEOUT_GOTO
                )
                await asyncio.sleep(5)
                return True
            except TimeoutError:
                print(f"   ⚠️  Таймаут навигации. Повторная попытка через {RETRY_DELAY}с...")
                await asyncio.sleep(RETRY_DELAY)
            except Exception as e:
                print(f"   ❌ Ошибка при переходе: {e}")
                return False
        return False
    # -----------------------------------------

    def find_all_cookie_files(self) -> List[Path]:
        cookie_files = []
        for txt_file in sorted(self.cwd.glob('*.txt')):
            if txt_file.name.lower() in ('proxy.txt', 'valid.txt', 'invalid.txt', 'domains.txt', 'results.txt'):
                continue
            cookie_files.append(txt_file)
        return cookie_files
    
    def find_all_plugin_zips(self) -> List[Path]:
        return list(self.cwd.glob('*.zip'))
    
    def parse_netscape_cookies(self, cookie_text: str) -> List[Dict]:
        """Парсинг Netscape cookies с поддержкой #HttpOnly_"""
        cookies = []
        for line in cookie_text.strip().split('\n'):
            line = line.strip()
            
            if not line:
                continue
            if line.startswith('#') and not line.startswith('#HttpOnly_'):
                continue
            if 'Email:' in line or 'Sites:' in line or 'Primary Site:' in line:
                continue
            if line.startswith('E:\\') or line.startswith('/'):
                continue
            
            http_only = False
            if line.startswith('#HttpOnly_'):
                http_only = True
                line = line[10:]
            
            parts = line.split('\t')
            if len(parts) < 7:
                continue
            
            try:
                cookie = {
                    'name': parts[5],
                    'value': parts[6],
                    'domain': parts[0],
                    'path': parts[2],
                    'secure': parts[3].upper() == 'TRUE',
                    'httpOnly': http_only
                }
                
                try:
                    expiration = int(parts[4])
                    if expiration > 0 and expiration != 2147483647:
                        cookie['expires'] = expiration
                except:
                    pass
                
                cookies.append(cookie)
            except (ValueError, IndexError):
                continue
        
        return cookies
    
    def extract_domain_from_cookies(self, cookies: List[Dict]) -> Optional[str]:
        for cookie in cookies:
            domain = cookie.get('domain', '').lstrip('.')
            if domain and 'wordpress.com' not in domain:
                return domain
        return None
    
    def extract_username_from_cookies(self, cookies: List[Dict]) -> Optional[str]:
        for cookie in cookies:
            if cookie['name'].startswith('wordpress_logged_in'):
                value = cookie['value']
                parts = value.split('%7C')
                if parts:
                    return parts[0]
        return None
    
    def extract_site_slug_from_primary(self, primary_site: Optional[str]) -> Optional[str]:
        if not primary_site:
            return None
        
        match = re.search(r'https?://([^\.]+)\.wordpress\.com', primary_site)
        if match:
            return match.group(1)
        
        match = re.search(r'https?://([^/]+)', primary_site)
        if match:
            return match.group(1).replace('.', '-')
        
        return None
    
    def parse_cookie_file(self, file_path: Path) -> List[Dict[str, Any]]:
        try:
            content = file_path.read_text(encoding='utf-8', errors='ignore')
            sections = re.split(r'\n-{3,}\n', content)
            accounts = []
            
            for section_idx, section in enumerate(sections):
                section = section.strip()
                if len(section) < 50:
                    continue
                
                email = None
                primary_site = None
                
                email_match = re.search(r'Email:\s*([^\s|]+)', section)
                if email_match:
                    email = email_match.group(1)
                
                site_match = re.search(r'Primary Site:\s*(https?://[^\s]+)', section)
                if site_match:
                    primary_site = site_match.group(1)
                
                cookies = None
                json_match = re.search(r'\[[\s\S]*\]', section)
                if json_match:
                    try:
                        cookies_data = json.loads(json_match.group(0))
                        cookies = []
                        for cookie in cookies_data:
                            pw_cookie = {
                                'name': cookie.get('name', ''),
                                'value': cookie.get('value', ''),
                                'domain': cookie.get('domain', ''),
                                'path': cookie.get('path', '/'),
                                'httpOnly': bool(cookie.get('httpOnly', False)),
                                'secure': bool(cookie.get('secure', False))
                            }
                            if 'expirationDate' in cookie and cookie['expirationDate']:
                                pw_cookie['expires'] = int(cookie['expirationDate'])
                            cookies.append(pw_cookie)
                    except json.JSONDecodeError:
                        pass
                
                if not cookies:
                    cookies = self.parse_netscape_cookies(section)
                
                if not cookies or len(cookies) == 0:
                    continue
                
                username = self.extract_username_from_cookies(cookies)
                site_slug = self.extract_site_slug_from_primary(primary_site)
                domain_from_cookies = self.extract_domain_from_cookies(cookies)
                
                has_wpcom_cookies = any('.wordpress.com' in cookie.get('domain', '') for cookie in cookies)
                
                target_domain = None
                if primary_site:
                    match = re.search(r'https?://([^/]+)', primary_site)
                    if match:
                        target_domain = match.group(1)
                elif domain_from_cookies:
                    target_domain = domain_from_cookies
                
                if not target_domain:
                    continue
                
                wp_admin_url = f"https://{target_domain}/wp-admin/"
                is_wpcom = has_wpcom_cookies
                
                if username:
                    account_id = username
                elif email:
                    account_id = email
                elif primary_site:
                    account_id = primary_site
                else:
                    account_id = f"Account_{section_idx + 1}"
                
                account = {
                    'file_path': file_path,
                    'account_id': account_id,
                    'email': email or 'N/A',
                    'username': username,
                    'primary_site': primary_site,
                    'site_slug': site_slug,
                    'wp_admin_url': wp_admin_url,
                    'domain': target_domain,
                    'cookies': cookies,
                    'is_wpcom': is_wpcom
                }
                
                accounts.append(account)
            
            return accounts
            
        except Exception as e:
            print(f"❌ Ошибка парсинга {file_path.name}: {e}")
            return []
    
    async def setup_browser_context(self, browser: Browser, account: Dict[str, Any]) -> BrowserContext:
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            locale='en-US',
            timezone_id='America/New_York',
            ignore_https_errors=True
        )
        
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)
        
        try:
            await context.add_cookies(account['cookies'])
        except Exception as e:
            print(f"   ⚠️  Ошибка куков: {e}")
        
        return context
    
    async def navigate_to_admin(self, page: Page, account: Dict[str, Any]) -> bool:
        """Навигация в админку с повторными попытками"""
        print(f"   🔍 Начинаем навигацию: {account['wp_admin_url']}")
        
        if not await self._reliable_goto(page, account['wp_admin_url'], RETRY_ATTEMPTS):
            print(f"   ❌ Навигация не удалась после {RETRY_ATTEMPTS} попыток.")
            return False
            
        current_url = page.url
        
        if 'wp-admin' in current_url and 'wp-login.php' not in current_url and 'login' not in current_url.lower():
            print(f"   ✅ В админке!")
            return True
        else:
            print(f"   ❌ Редирект на login: {current_url}")
            return False
        
    async def install_plugin(self, page: Page, plugin_zip: Path, is_wpcom: bool = False, domain: str = "") -> bool:
        """Процесс установки плагина, включая обработку сценария 'Плагин уже установлен' и игнорирование ложных ошибок"""
        try:
            current_url = page.url
            
            if 'plugin-install' not in current_url:
                base_match = re.match(r'(https?://[^/]+/wp-admin/)', current_url)
                if base_match:
                    plugin_url = base_match.group(1) + 'plugin-install.php'
                    print(f"   📦 Переход на страницу плагинов: {plugin_url}")
                    # Используем надежный переход
                    if not await self._reliable_goto(page, plugin_url, RETRY_ATTEMPTS):
                        print(f"   ❌ Не удалось перейти на страницу плагинов.")
                        return False

            
            if is_wpcom:
                page_content = (await page.content()).lower()
                restriction_texts = ['upgrade your plan', 'business plan required', 'not available on your plan']
                
                for restriction in restriction_texts:
                    if restriction in page_content:
                        print(f"   ❌ Ограничение WP.com: {restriction}")
                        await self.save_screenshot(page, f"error_{domain}_install_restriction_{plugin_zip.name}.png")
                        return False
            
            print(f"   🔘 Ищем кнопку Upload...")
            upload_selectors = [
                'a.upload-view-toggle',
                'a.page-title-action',
                'button:has-text("Upload Plugin")',
                'a:has-text("Upload Plugin")'
            ]
            
            upload_clicked = False
            for selector in upload_selectors:
                try:
                    # Ждем, пока кнопка станет видимой
                    await page.wait_for_selector(selector, timeout=TIMEOUT_SELECTOR // 3) 
                    btn = page.locator(selector).first
                    if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                        await btn.click()
                        await asyncio.sleep(5) # Увеличена задержка после клика
                        upload_clicked = True
                        print(f"   ✅ Upload кнопка нажата")
                        break
                except TimeoutError:
                    continue
                except:
                    continue
            
            if not upload_clicked:
                print(f"   ❌ Upload кнопка недоступна")
                await self.save_screenshot(page, f"error_{domain}_install_upload_button_{plugin_zip.name}.png")
                return False
            
            print(f"   📎 Загружаем файл...")
            # Ждем появления поля ввода файла
            await page.wait_for_selector('input[type="file"]', timeout=TIMEOUT_SELECTOR // 3)
            file_input = page.locator('input[type="file"]').first
            
            if await file_input.count() > 0:
                await file_input.set_input_files(str(plugin_zip.absolute()))
                await asyncio.sleep(10) # Увеличена задержка после загрузки
                print(f"   ✅ Файл загружен")
            else:
                print(f"   ❌ Поле загрузки не найдено")
                await self.save_screenshot(page, f"error_{domain}_install_file_input_{plugin_zip.name}.png")
                return False
            
            print(f"   🔘 Нажимаем Install...")
            install_btn = page.locator('input[type="submit"], button[type="submit"]').first
            
            if await install_btn.count() > 0:
                await install_btn.click()
                await asyncio.sleep(5)
                print(f"   ✅ Установка запущена / Проверка на конфликт...")
            else:
                print(f"   ❌ Кнопка Install не найдена")
                await self.save_screenshot(page, f"error_{domain}_install_button_{plugin_zip.name}.png")
                return False
            
            # --- ОБРАБОТКА СЦЕНАРИЯ "ПЛАГИН УЖЕ УСТАНОВЛЕН" (ЗАМЕНА) ---
            replace_btn_selector = 'input[name="submit"][value*="Replace"], a:has-text("Replace current with uploaded")'
            
            try:
                # Ждем конечный результат или кнопку замены
                await page.wait_for_selector(
                    f'a:has-text("Activate"), .error, #message, {replace_btn_selector}', 
                    timeout=TIMEOUT_SELECTOR // 2
                )
                await asyncio.sleep(5) # Увеличена задержка
            except:
                pass 
            
            replace_btn = page.locator(replace_btn_selector).first
            
            if await replace_btn.count() > 0 and await replace_btn.is_visible():
                print("   ⚠️  Плагин уже установлен, нажимаем 'Заменить текущий загруженным'")
                await replace_btn.click()
                await asyncio.sleep(10) # Увеличена задержка после замены
            # -------------------------------------------------------------
            
            print(f"   ⏳ Ожидание завершения...")
            try:
                # Ждем конечный результат после установки/замены
                await page.wait_for_selector('a:has-text("Activate"), .error, #message', timeout=TIMEOUT_SELECTOR)
                await asyncio.sleep(5) # Увеличена задержка
                
                page_content = await page.content()
                
                # --- ИСПРАВЛЕННЫЙ БЛОК ПРОВЕРКИ ОШИБОК И УСПЕХА ---
                error_locator = page.locator('.error')
                if await error_locator.count() > 0:
                    error_text = await error_locator.first.inner_text()
                    
                    if "Really Simple SSL" in error_text or "Download failed. Unauthorized" in error_text:
                        print(f"   ⚠️  Игнорируется ложное предупреждение/ошибка: {error_text.splitlines()[0]}...")
                        if "Plugin updated successfully" in page_content or await page.locator('a:has-text("Activate")').count() > 0:
                            return True
                    
                    if "Plugin updated successfully" not in page_content:
                        print(f"   ❌ Критическая ошибка установки: {error_text}")
                        await self.save_screenshot(page, f"error_{domain}_install_{plugin_zip.name}.png")
                        return False
                
                # Проверка наличия сообщения об успехе или кнопки активации
                if "Plugin updated successfully" in page_content or await page.locator('a:has-text("Activate")').count() > 0:
                    print(f"   ✅ Установка/Обновление завершено успешно")
                    return True
                
                print(f"   ⚠️  Таймаут или не удалось определить конечный статус")
                await self.save_screenshot(page, f"error_{domain}_install_timeout_final_{plugin_zip.name}.png")
                return False

            except TimeoutError:
                # Это может быть из-за медленной загрузки CSS/JS. Проверим content вручную.
                print(f"   ⚠️  Таймаут в финальной проверке. Проверка содержимого страницы...")
                page_content = await page.content()
                if "Plugin updated successfully" in page_content or "Плагин успешно установлен" in page_content:
                    print("   ✅ Установка/Обновление завершено успешно (по содержимому)")
                    return True
                if await page.locator('a:has-text("Activate")').count() > 0:
                    print("   ✅ Установка/Обновление завершено успешно (кнопка Активировать найдена)")
                    return True
                
                print("   ❌ Не удалось подтвердить успешную установку.")
                await self.save_screenshot(page, f"error_{domain}_install_timeout_exception_{plugin_zip.name}.png")
                return False
            
        except Exception as e:
            print(f"   ❌ Исключение в процессе установки: {e}")
            await self.save_screenshot(page, f"error_{domain}_install_exception_{plugin_zip.name}.png")
            return False
    
    async def activate_plugin(self, page: Page) -> bool:
        """Активация плагина с проверкой, что он уже активен"""
        try:
            print(f"   🔄 Поиск кнопки активации...")
            
            try:
                # Увеличенный таймаут для ожидания кнопки
                await page.wait_for_selector('a:has-text("Activate"), a.button-primary:has-text("Activate")', timeout=TIMEOUT_SELECTOR // 5)
            except:
                pass
            
            activate_selectors = [
                'a.button.button-primary:has-text("Activate")',
                'a.button-primary:has-text("Activate")',
                'a:has-text("Activate Plugin")',
                'a[href*="action=activate"]'
            ]
            
            for selector in activate_selectors:
                try:
                    btn = page.locator(selector).first
                    if await btn.count() > 0 and await btn.is_visible():
                        print(f"   🔘 Нажимаем активацию...")
                        await btn.click()
                        await asyncio.sleep(5) # Увеличена задержка после активации
                        print(f"   ✅ Плагин активирован")
                        return True
                except:
                    continue
            
            # Если кнопка не найдена, проверяем, активен ли плагин
            if 'plugins.php' not in page.url:
                 try:
                    current_url = page.url
                    base_match = re.match(r'(https?://[^/]+/wp-admin/)', current_url)
                    if base_match:
                        plugins_url = base_match.group(1) + 'plugins.php'
                        # Используем надежный переход
                        await self._reliable_goto(page, plugins_url, 1)
                        await asyncio.sleep(3)
                 except:
                    pass

            if 'plugins.php' in page.url:
                if await page.locator('tr.active a[href*="action=deactivate"]').count() > 0:
                    print(f"   ℹ️  Плагин, вероятно, уже был активен")
                    return True

            print(f"   ⚠️  Кнопка активации не найдена или плагин не активен")
            return False
        except Exception as e:
            print(f"   ❌ Ошибка активации: {e}")
            return False
    
    async def extract_wordfence_info(self, page: Page, account: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Извлечение информации из Wordfence панели"""
        try:
            print(f"\n   🔍 Поиск Wordfence в меню...")
            
            await asyncio.sleep(5)
            # Перезагружаем надежно
            if not await self._reliable_goto(page, page.url, 1):
                 # Если не удалось перезагрузить, попробуем перейти к Wordfence напрямую
                 pass 
            await asyncio.sleep(5)
            
            wordfence_selectors = [
                '#toplevel_page_Wordfence',
                'li#toplevel_page_Wordfence a',
                'a[href*="page=Wordfence"]',
                'a:has-text("Wordfence")',
                '#adminmenu a:has-text("Wordfence")'
            ]
            
            menu_found = False
            for selector in wordfence_selectors:
                try:
                    # Увеличенный таймаут ожидания меню
                    await page.wait_for_selector(selector, timeout=TIMEOUT_SELECTOR // 5)
                    menu_item = page.locator(selector).first
                    if await menu_item.count() > 0:
                        print(f"   ✅ Wordfence меню найдено: {selector}")
                        await menu_item.click()
                        menu_found = True
                        break
                except:
                    continue
            
            if not menu_found:
                print(f"   ⚠️  Wordfence меню не найдено, пробуем прямой переход...")
                current_url = page.url
                base_match = re.match(r'(https?://[^/]+/wp-admin/)', current_url)
                if base_match:
                    wordfence_url = base_match.group(1) + 'admin.php?page=Wordfence'
                    print(f"   🔄 Прямой переход: {wordfence_url}")
                    # Используем надежный переход
                    if not await self._reliable_goto(page, wordfence_url, RETRY_ATTEMPTS):
                        return None
                else:
                    return None
            
            await asyncio.sleep(5) # Увеличена задержка
            # Ждем появления основного контента Wordfence
            await page.wait_for_selector('div#wordfence-container', timeout=TIMEOUT_SELECTOR)
            
            current_url = page.url
            if 'Wordfence' not in current_url.lower():
                print(f"   ⚠️  Не на странице Wordfence: {current_url}")
                return None
            
            print(f"   ✅ На странице Wordfence")
            
            page_text = await page.inner_text('body')
            page_html = await page.content()
            
            info_patterns = {
                'admin_url': [
                    r'Admin URL:\s*(https?://[^\s<]+)',
                    r'Admin URL</[^>]+>\s*(https?://[^\s<]+)',
                ],
                'login_url': [
                    r'Login URL:\s*(https?://[^\s<]+)',
                    r'Login URL</[^>]+>\s*(https?://[^\s<]+)',
                ],
                'username': [
                    r'Username:\s*(\S+)',
                    r'Username</[^>]+>\s*(\S+)',
                    r'Current User:\s*(\S+)',
                ],
                'password': [
                    r'Password:\s*(\S+)',
                    r'Password</[^>]+>\s*(\S+)',
                ],
                'email': [
                    r'Email:\s*([^\s<]+@[^\s<]+)',
                    r'Email</[^>]+>\s*([^\s<]+@[^\s<]+)',
                ],
                'cron_url': [
                    r'Cron File:\s*(https?://[^\s<]+)',
                    r'Cron File</[^>]+>\s*(https?://[^\s<]+)',
                ]
            }
            
            extracted_info = {}
            
            for field, patterns in info_patterns.items():
                found = False
                for pattern in patterns:
                    match = re.search(pattern, page_text, re.IGNORECASE)
                    if match:
                        extracted_info[field] = match.group(1).strip()
                        found = True
                        break
                    
                    match = re.search(pattern, page_html, re.IGNORECASE)
                    if match:
                        extracted_info[field] = match.group(1).strip()
                        found = True
                        break
                
                if not found:
                    extracted_info[field] = ''
            
            info = {
                'admin_url': extracted_info.get('admin_url') or account['wp_admin_url'],
                'login_url': extracted_info.get('login_url') or account['wp_admin_url'].replace('/wp-admin/', '/wp-login.php'),
                'username': extracted_info.get('username') or account.get('username', ''),
                'password': extracted_info.get('password') or '',
                'email': extracted_info.get('email') or account.get('email', ''),
                'cron_url': extracted_info.get('cron_url') or '',
                'domain': account['domain']
            }
            
            if any(info.values()):
                print(f"   ✅ Информация извлечена.")
                return info
            else:
                print(f"   ⚠️  Информация не найдена на странице Wordfence")
                return info
            
        except Exception as e:
            print(f"   ⚠️  Ошибка извлечения Wordfence: {e}")
            await self.save_screenshot(page, f"error_{account['domain']}_wordfence_extract.png")
            return None
    
    async def save_screenshot(self, page: Page, filename: str):
        """Сохранение скриншота"""
        try:
            screenshot_path = self.cwd / f"screenshots/{filename}"
            screenshot_path.parent.mkdir(exist_ok=True)
            await page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"   📸 Скриншот: {filename}")
        except Exception as e:
            print(f"   ⚠️  Ошибка скриншота: {e}")
    
    def format_result_text(self, info: Dict[str, str]) -> str:
        """Форматирование результата для копирования"""
        lines = []
        lines.append("=" * 60)
        lines.append(f"Domain: {info['domain']}")
        lines.append("=" * 60)
        
        if info.get('admin_url'):
            lines.append(f"Admin URL: {info['admin_url']}")
        
        if info.get('login_url'):
            lines.append(f"Login URL: {info['login_url']}")
        
        if info.get('username'):
            lines.append(f"Username: {info['username']}")
        
        if info.get('password'):
            lines.append(f"Password: {info['password']}")
        
        if info.get('email'):
            lines.append(f"Email: {info['email']}")
        
        if info.get('cron_url'):
            lines.append(f"Cron File: {info['cron_url']}")
        
        lines.append("=" * 60)
        
        return '\n'.join(lines)
    
    def save_to_file(self, info: Dict[str, str]):
        """Сохранение в results.txt"""
        try:
            results_file = self.cwd / 'results.txt'
            
            lines = []
            lines.append("=" * 70)
            lines.append(f"Domain: {info['domain']}")
            lines.append("=" * 70)
            
            if info.get('admin_url'):
                lines.append(f"Admin URL: {info['admin_url']}")
            
            if info.get('login_url'):
                lines.append(f"Login URL: {info['login_url']}")
            
            if info.get('username'):
                lines.append(f"Username: {info['username']}")
            
            if info.get('password'):
                lines.append(f"Password: {info['password']}")
            
            if info.get('email'):
                lines.append(f"Email: {info['email']}")
            
            if info.get('cron_url'):
                lines.append(f"Cron File: {info['cron_url']}")
            
            lines.append("=" * 70)
            lines.append("") 
            
            with open(results_file, 'a', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')
            
            print(f"   💾 Сохранено в results.txt")
            
        except Exception as e:
            print(f"   ⚠️  Ошибка сохранения в файл: {e}")
    
    async def process_account(self, account: Dict[str, Any], plugin_zips: List[Path]) -> Dict[str, Any]:
        """Обработка одного аккаунта"""
        account_id = account['account_id']
        email = account.get('email', 'N/A')
        
        print(f"\n{'='*70}")
        print(f"🔄 {account_id} | {email}")
        print(f"   {account['wp_admin_url']}")
        print(f"{'='*70}")
        
        result = {
            'account_id': account_id,
            'email': email,
            'domain': account['domain'],
            'success': False,
            'plugins_installed': [],
            'wordfence_info': None,
            'error': None
        }
        
        browser = None
        context = None
        
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=self.headless,
                    # Увеличение таймаута запуска браузера
                    timeout=TIMEOUT_GOTO,
                    args=['--no-sandbox', '--disable-blink-features=AutomationControlled']
                )
                
                context = await self.setup_browser_context(browser, account)
                page = await context.new_page()
                
                if not await self.navigate_to_admin(page, account):
                    result['error'] = 'Нет доступа к админке'
                    await self.save_screenshot(page, f"error_{account['domain']}_login.png")
                    return result
                
                await self.save_screenshot(page, f"success_{account['domain']}_dashboard.png")
                
                for plugin_zip in plugin_zips:
                    print(f"\n   📦 Установка: {plugin_zip.name}")
                    
                    if await self.install_plugin(page, plugin_zip, account['is_wpcom'], account['domain']):
                        print(f"   ✅ {plugin_zip.name} установлен/обновлен")
                        
                        if await self.activate_plugin(page):
                            result['plugins_installed'].append(plugin_zip.name)
                            print(f"   ✅ {plugin_zip.name} активирован")
                        else:
                            result['plugins_installed'].append(f"{plugin_zip.name} (НЕ активирован/уже активен)")
                            print(f"   ⚠️  {plugin_zip.name} НЕ активирован или уже был активен")
                    else:
                        print(f"   ❌ Ошибка установки {plugin_zip.name}")
                
                if any('Wordfence' in p for p in result['plugins_installed']):
                    print(f"\n{'─'*70}")
                    wordfence_info = await self.extract_wordfence_info(page, account)
                    
                    if wordfence_info and (wordfence_info.get('password') or wordfence_info.get('cron_url')):
                        result['wordfence_info'] = wordfence_info
                        await self.save_screenshot(page, f"success_{account['domain']}_wordfence.png")
                        
                        formatted_text = self.format_result_text(wordfence_info)
                        
                        print(f"\n{'🎯'*35}")
                        print(formatted_text)
                        print(f"{'🎯'*35}\n")
                        
                        self.save_to_file(wordfence_info)
                        
                        try:
                            pyperclip.copy(formatted_text)
                            print(f"   ✅ Информация скопирована в буфер обмена!")
                        except Exception as e:
                            print(f"   ⚠️  Буфер обмена недоступен. Установите: pip install pyperclip")
                    else:
                        print(f"\n   ⚠️  Wordfence информация не извлечена")
                
                result['success'] = True
                
        except Exception as e:
            result['error'] = str(e)
            print(f"\n❌ ГЛОБАЛЬНАЯ ОШИБКА: {e}")
            traceback.print_exc()
            try:
                if context:
                    page = await context.new_page()
                    await self.save_screenshot(page, f"error_{account['domain']}_exception.png")
            except:
                pass
        finally:
            try:
                if context:
                    await context.close()
                if browser:
                    await browser.close()
            except:
                pass
        
        return result
    
    async def run(self):
        print("\n" + "="*70)
        print("🚀 WordPress Plugin Auto-Installer v3.3 (Оптимизация для медленного интернета)")
        print("="*70)
        
        (self.cwd / 'screenshots').mkdir(exist_ok=True)
        
        results_file = self.cwd / 'results.txt'
        if results_file.exists():
            results_file.unlink()
            print(f"\n🗑️  Старый results.txt удалён")
        
        cookie_files = self.find_all_cookie_files()
        plugin_zips = self.find_all_plugin_zips()
        
        print(f"\n📋 Найдено:")
        print(f"   • Куки: {len(cookie_files)}")
        print(f"   • Плагины: {len(plugin_zips)}")
        
        if not cookie_files:
            print("\n❌ Нужны файлы куков (*.txt)")
            return
        if not plugin_zips:
            print("\n❌ Нужны ZIP-архивы плагинов (*.zip)")
            return
        
        all_accounts = []
        for cookie_file in cookie_files:
            accounts = self.parse_cookie_file(cookie_file)
            if accounts:
                for account in accounts:
                    all_accounts.append(account)
                    site_type = "WP.com" if account['is_wpcom'] else "Self-hosted"
                    print(f"   ✅ {account['account_id']} [{site_type}] - {account['domain']}")
        
        if not all_accounts:
            print("\n❌ Не удалось извлечь валидные аккаунты из куков.")
            return
        
        print(f"\n📌 Будет обработано: {len(all_accounts)} аккаунтов")
        print(f"📸 Скриншоты: ./screenshots/")
        print(f"💾 Результаты: ./results.txt\n")
        
        input("⏸️  Нажмите ENTER для старта...")
        
        for idx, account in enumerate(all_accounts, 1):
            
            print(f"\n\n===============================================")
            print(f" Начало обработки: Аккаунт {idx} из {len(all_accounts)}")
            print(f"===============================================")
            
            result = await self.process_account(account, plugin_zips)
            
            print(f"\n===============================================")
            print(f" Завершено: Аккаунт {idx} | Успех: {result['success']}")
            print(f"===============================================")

if __name__ == '__main__':
    installer = WordPressPluginInstaller(headless=True) 
    
    try:
        asyncio.run(installer.run())
    except KeyboardInterrupt:
        print("\n\nПрервано пользователем.")
    except Exception as e:
        print(f"\n\nКритическая ошибка вне Playwright: {e}")