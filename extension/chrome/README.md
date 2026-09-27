# Chrome extension mode

1. Run `pip install -r requirements.txt`.
2. Set `bot.browser.mode: "extension"` in `config/config.yaml`.
3. Start the bot once. It creates `data/extension_pairing_token` and waits.
4. Open `chrome://extensions`, enable **Developer mode**, choose **Load unpacked**,
   and select this `extension/chrome` directory.
5. Open the extension's **Options**, paste the token, and save.

Indeed domains are allowed by default. Enable the separate company-site option
only if you use that existing feature; Chrome will show an explicit permission
prompt. The extension creates/reuses only dedicated automation tabs. The Python
bridge binds only to loopback and rejects clients without the shared token.
Screenshot support is another explicit option. Chrome requests optional
all-sites access because its visible-tab capture API requires it. The extension
captures only the visible area of its dedicated automation tab and restores the
previously active tab afterward.
