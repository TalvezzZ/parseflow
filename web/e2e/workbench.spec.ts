import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const fixturesDir = path.join(path.dirname(fileURLToPath(import.meta.url)), 'fixtures')

test('real backend upload, deep link, artifact, retry and delete', async ({ page }) => {
  await page.goto('/')
  await page.locator('input[type=file]').setInputFiles({ name: 'e2e.txt', mimeType: 'text/plain', buffer: Buffer.from('ParseFlow browser E2E') })
  await page.getByRole('button', { name: /开始智能解析/ }).click()
  await expect(page.getByRole('heading', { level: 2, name: '解析完成' })).toBeVisible()
  await expect(page).toHaveURL(/\/tasks\/task_[0-9a-f]{32}$/)
  const deepLink = page.url()
  await page.reload()
  await expect(page).toHaveURL(deepLink)
  await expect(page.getByRole('heading', { level: 2, name: '解析完成' })).toBeFocused()

  await page.getByRole('tab', { name: /产物/ }).click()
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('link', { name: '下载' }).first().click()
  expect((await downloadPromise).suggestedFilename()).toBeTruthy()

  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations.filter((item) => ['critical', 'serious'].includes(item.impact || ''))).toEqual([])

  const previousTaskUrl = page.url()
  await page.getByRole('button', { name: '重试' }).click()
  await expect(page).not.toHaveURL(previousTaskUrl)
  await expect(page).toHaveURL(/\/tasks\/task_[0-9a-f]{32}$/)
  await expect(page.getByRole('heading', { level: 2, name: '解析完成' })).toBeVisible()
  await page.getByRole('button', { name: '删除' }).click()
  await expect(page.getByRole('heading', { level: 2, name: '任务中心' })).toBeVisible()
})

test('OCR proofing renders coordinates for a real enhanced-mode parse', async ({ page }) => {
  // Uses local PaddleOCR against a generated high-contrast fixture so the real
  // OCR path, page preview and coordinate overlay are exercised end to end.
  test.slow()
  await page.goto('/')
  await page.getByRole('button', { name: /智能默认/ }).click()
  await page.getByRole('option', { name: /增强解析/ }).click()
  await page.locator('input[type=file]').setInputFiles(path.join(fixturesDir, 'ocr-sample.png'))
  await page.getByRole('button', { name: /开始智能解析/ }).click()

  await expect(page.getByRole('heading', { level: 2, name: '解析完成' })).toBeVisible({ timeout: 180_000 })
  await page.getByRole('tab', { name: 'OCR 校对' }).click()
  await expect(page.getByRole('heading', { name: '版面识别校对' })).toBeVisible()
  // The overlay renders one focusable region per recognized text block, anchored
  // to a stage whose aspect ratio comes from the OCR page dimensions.
  await expect(page.locator('.ocr-image-stage')).toBeVisible()
  await expect(page.locator('.ocr-overlay button').first()).toBeVisible()
  await expect(page.getByText(/\d+ 个文本块/)).toBeVisible()
})
