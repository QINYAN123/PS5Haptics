"""Version, author and project links (sidebar, update check, docs, release packaging)."""
VERSION = '2.3.7'
AUTHOR = '轻言black'
GITHUB_USER = 'QINYAN123'
GITHUB_REPO = 'QINYAN123/PS5Haptics'
GITHUB_URL = 'https://github.com/' + GITHUB_REPO
RELEASES_URL = GITHUB_URL + '/releases'
RELEASES_API = 'https://api.github.com/repos/' + GITHUB_REPO + '/releases/latest'
BILIBILI_URL = 'https://space.bilibili.com/65980286'
UPDATE_ASSET_HINT = '更新包'          # local file name of the update package
# release asset names on GitHub (ASCII: GitHub strips other characters from asset names)
FULL_ASSET = 'PS5Haptics-v{}.zip'
UPDATE_ASSET = 'PS5Haptics-update-v{}.zip'
