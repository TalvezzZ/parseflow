import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

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
