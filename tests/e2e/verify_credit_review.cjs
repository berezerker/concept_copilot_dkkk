// Проверка маршрута инспектора на переданной обезличенной книге в Microsoft Excel.
const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');

const [url, book, firstPeriod, secondPeriod, input, inputPeriod] = process.argv.slice(2);
if (![url, book, firstPeriod, secondPeriod, input, inputPeriod].every(Boolean)) {
  throw new Error('Usage: node verify_credit_review.cjs URL BOOK PERIOD_A PERIOD_B INPUT_A1 INPUT_PERIOD');
}
const digest = () => createHash('sha256').update(readFileSync(book)).digest('hex');

async function main() {
  const original = digest();
  const browser = await chromium.launch({channel: 'chrome', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: Number(process.env.CF_UI_WIDTH || 1280), height: 900}});
    page.setDefaultTimeout(180000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      localStorage.setItem('cf_provider', 'mock');
      localStorage.setItem('cf_tour_seen', '1');
    });
    await page.goto(url);
    const reviewLoaded = page.waitForResponse(r => /\/api\/credit-review\/[^/?]+$/.test(r.url()));
    await page.locator('#file').setInputFiles(book);
    const reviewResponse = await reviewLoaded;
    assert.equal(reviewResponse.status(), 200, await reviewResponse.text());
    const model = await reviewResponse.json();
    assert.equal(model.readiness.engine, 'excel');
    assert.ok(model.checks.length);
    assert.equal(model.confirmed_covenant, null);
    const workbench = page.locator('.credit-workbench');
    await workbench.getByRole('button', {name: 'Перейти к проверке'}).click();
    await workbench.getByText('Тип модели и отрасль', {exact: true}).click();
    const profilesSaved = page.waitForResponse(r => r.url().endsWith('/profiles'));
    await workbench.getByRole('button', {name: 'Подтвердить профили'}).click();
    const profiles = await (await profilesSaved).json();
    assert.ok(profiles.profiles.filter(x => x.included).every(x => x.status === 'confirmed'));
    const debt = workbench.locator('.review-finding').filter({hasText: 'График обслуживания долга'});
    await debt.locator('summary').click();
    await debt.locator('[name=decision]').selectOption('no');
    await debt.locator('[data-applicability] [name=source]').fill('Тестовое исключение');
    const decision = page.waitForResponse(r => r.url().endsWith('/checks/debt_schedule'));
    await debt.getByRole('button', {name: 'Сохранить решение'}).click();
    const rules = await (await decision).json();
    assert.equal(rules.checks.find(item => item.id === 'debt_schedule').status, 'not_applicable');
    const audit = workbench.locator('[data-reconciliation]');
    await audit.getByText('Сверить расчёт EBITDA или DSCR', {exact: true}).click();
    await audit.locator('[name=definition]').selectOption('ebitda');
    await audit.locator('[name=period]').fill(firstPeriod);
    await audit.locator('[name=period]').dispatchEvent('change');
    await audit.locator('[name=sign]').selectOption('1');
    await audit.locator('[name=source]').fill('Тест: EBIT плюс амортизация');
    await audit.locator('[name=basis]').check();
    const reconciliation = page.waitForResponse(r => r.url().endsWith('/reconcile'));
    await audit.getByRole('button', {name: 'Сверить по Excel'}).click();
    const arithmetic = await (await reconciliation).json();
    assert.equal(arithmetic.period, firstPeriod);
    assert.equal(arithmetic.actual, arithmetic.evidence[0].value);
    assert.equal(arithmetic.calculated, arithmetic.evidence[1].value + arithmetic.evidence[2].value);
    const covenant = workbench.locator('[data-covenant]');
    assert.equal(await covenant.getByRole('button', {name: 'Проверить базу Excel'}).isVisible(), false);
    await covenant.locator('[name=metric]').selectOption('dscr');
    const metric = model.covenants.find(item => item.id === 'dscr');
    const binding = metric.bindings.findIndex(row => row.periods.includes(firstPeriod) && row.periods.includes(secondPeriod));
    assert.ok(binding >= 0);
    await covenant.locator('[name=binding]').selectOption(String(binding));
    await covenant.locator(`[name=periods][value="${firstPeriod}"]`).check();
    await covenant.locator(`[name=periods][value="${secondPeriod}"]`).check();
    await covenant.locator('[name=threshold]').fill('1.2');
    await covenant.locator('[name=source]').fill('Тестовое условие сделки, пункт 5.2');
    await covenant.getByRole('button', {name: 'Подтвердить условия'}).click();
    const baseline = page.waitForResponse(r => r.request().method() === 'GET' && r.url().endsWith('/covenant'));
    await covenant.getByRole('button', {name: 'Проверить базу Excel'}).click();
    const base = await (await baseline).json();
    assert.equal(base.series.length, 2);
    assert.equal(base.rule.source, 'Тестовое условие сделки, пункт 5.2');
    await covenant.getByRole('button', {name: 'Подготовить стресс по ковенанту'}).click();
    const form = workbench.locator('.scenario-builder');
    await form.locator('[data-field=source]').fill(input);
    await form.locator('[data-inputs] [data-field=period]').fill(inputPeriod);
    const op = process.env.CF_SHOCK_OP || 'pct';
    const amount = Number(process.env.CF_SHOCK_AMOUNT || '-10');
    await form.locator('[data-field=op]').selectOption(op);
    if (op === 'pp') await form.locator('[data-field=percent-scale]').selectOption('fraction');
    await form.locator('[data-field=pct]').fill(String(amount));
    const experiment = form.getByText('Связь не видна в формулах?', {exact: true});
    await experiment.click();
    await form.locator('[name=experiment]').fill('Проверить отклик выбранных периодов при изменении входа');
    await form.getByRole('button', {name: 'Проверить план'}).click();
    await form.getByRole('heading', {name: 'План Excel'}).waitFor();
    await form.getByRole('button', {name: 'Пересчитать в Excel'}).click();
    await form.getByText('Откат подтверждён после каждого пересчёта.', {exact: true}).waitFor();
    const conclusion = page.waitForResponse(r => r.url().includes('/conclusion?plan_id='));
    await workbench.getByRole('button', {name: 'Перейти к выводу'}).click();
    const draft = await (await conclusion).json();
    assert.equal(draft.run.rollback_verified, true);
    const observed = draft.run.combined.inputs[0];
    if (op === 'pp') assert.ok(Math.abs(observed.achieved_pp - amount) < 1e-9);
    else if (op === 'set') assert.equal(observed.after, amount);
    else assert.ok(Math.abs(observed.achieved_pct - amount) < 1e-9);
    assert.deepEqual(draft.covenant.series.map(x => x.ref), base.series.map(x => x.ref));
    for (const period of draft.covenant.series) {
      assert.equal(period.headroom, period.after - 1.2);
    }
    assert.match(draft.markdown, /Откат Excel подтверждён/);
    assert.match(draft.markdown, /Тестовое условие сделки/);
    await workbench.getByRole('button', {name: 'Скачать черновик'}).waitFor();
    const exported = page.waitForEvent('download');
    await workbench.getByRole('button', {name: 'Скачать Word'}).click();
    const download = await exported;
    assert.match(download.suggestedFilename(), /\.docx$/);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({status: 'PASS', model: model.model.sheets, profiles: model.profiles.filter(x => x.included).map(x => x.id),
                                covenant: draft.covenant.status, periods: draft.covenant.series.map(x => x.period),
                                op, rollback: true}));
  } finally {
    await browser.close();
    assert.equal(digest(), original, 'Исходная книга изменилась');
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
