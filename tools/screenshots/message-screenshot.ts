/* global $, CSS */

import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import path from "node:path";
import {parseArgs} from "node:util";

import "css.escape";
import * as puppeteer from "puppeteer";
import * as z from "zod/mini";

const usage = "Usage: message-screenshot.ts <message_id> <image_path> <realm_url>";
const {
    values: {help},
    positionals,
} = parseArgs({options: {help: {type: "boolean"}}, allowPositionals: true});

if (help) {
    console.log(usage);
    process.exit(0);
}

const parsed = z
    .tuple([
        z.string(),
        z.templateLiteral([z.string(), z.enum([".png", ".jpeg", ".webp"])]),
        z.url(),
    ])
    .safeParse(positionals);
if (!parsed.success) {
    console.error(usage);
    process.exit(1);
}
const [messageId, imagePath, realmUrl] = parsed.data;

console.log(`Capturing screenshot for message ${messageId} to ${imagePath}`);

// TODO: Refactor to share code with web/e2e-tests/realm-creation.test.ts
async function run(): Promise<void> {
    const browser = await puppeteer.launch({
        args: [
            "--window-size=1400,1024",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            // Helps render fonts correctly on Ubuntu: https://github.com/puppeteer/puppeteer/issues/661
            "--font-render-hinting=none",
        ],
        defaultViewport: null,
        headless: true,
    });
    try {
        const page = await browser.newPage();
        // deviceScaleFactor:2 gives better quality screenshots (higher pixel density)
        await page.setViewport({width: 1280, height: 1024, deviceScaleFactor: 2});

        // Log in via dev endpoint to avoid host-redirect issues with `/devlogin`.
        await page.goto(`${realmUrl}/`, {waitUntil: "domcontentloaded"});
        await page.evaluate(async (username) => {
            const body = new URLSearchParams({username});
            const res = await fetch("/api/v1/dev_fetch_api_key", {
                method: "POST",
                headers: {"Content-Type": "application/x-www-form-urlencoded"},
                body,
                credentials: "same-origin",
            });
            if (!res.ok) {
                throw new Error(`dev_fetch_api_key failed: ${res.status}`);
            }
        }, "iago@zulip.com");

        // Navigate to message and capture screenshot
        await page.goto(`${realmUrl}/#narrow/id/${messageId}`, {
            waitUntil: "networkidle2",
        });
        const dataSelector = `[data-message-id="${messageId}"]`;
        await page.waitForSelector(dataSelector);
        const message_row_id = await page.evaluate((sel) => {
            const el = document.querySelector<HTMLElement>(sel);
            return el?.id;
        }, dataSelector);
        assert.ok(message_row_id);

        const messageSelector = `#${CSS.escape(message_row_id)}`;

        // Remove unread marker and don't select message.
        // (Don't rely on jQuery globals; not all builds expose `$` on window.)
        const marker = `${messageSelector} .unread_marker`;
        await page.evaluate((sel) => {
            for (const el of document.querySelectorAll(sel)) {
                el.remove();
            }
        }, marker);
        const messageBox = await page.$(messageSelector);
        assert.ok(messageBox !== null);
        await page.evaluate((sel) => {
            const el = document.querySelector(sel);
            if (el) {
                el.classList.remove("selected_message");
            }
        }, messageSelector);
        const messageGroup = await messageBox.$("xpath/..");
        assert.ok(messageGroup !== null);
        // Compute screenshot area, with some padding around the message group
        const box = await messageGroup.boundingBox();
        assert.ok(box !== null);
        const imageDir = path.dirname(imagePath);
        await fs.promises.mkdir(imageDir, {recursive: true});
        await page.screenshot({
            path: imagePath,
            clip: {x: box.x - 5, y: box.y + 5, width: box.width + 10, height: box.height},
        });
    } finally {
        await browser.close();
    }
}

await run();
