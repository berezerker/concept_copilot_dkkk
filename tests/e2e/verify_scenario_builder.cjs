// Playwright smoke test for a running local app with Microsoft Excel.
const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const {readFileSync} = require('node:fs');
const {chromium} = require('playwright');

const [url, model, metric, driverA, driverB, periodA, periodB] = process.argv.slice(2);
if (![url, model, metric, driverA, driverB, periodA, periodB].every(Boolean)) {
  throw new Error('Usage: node verify_scenario_builder.cjs URL MODEL METRIC DRIVER_A DRIVER_B PERIOD_A PERIOD_B');
}
const digest = () => createHash('sha256').update(readFileSync(model)).digest('hex');

async function main() {
  const original = digest();
  const browser = await chromium.launch({channel:'chrome', headless:true});
  try {
    const page = await browser.newPage({viewport:{width:Number(process.env.CF_UI_WIDTH || 1280),height:900}});
    page.setDefaultTimeout(60000);
    await page.addInitScript(() => {
      localStorage.setItem('cf_provider','mock');
      localStorage.setItem('cf_tour_seen','1');
    });
    await page.goto(url);
    await page.locator('#file').setInputFiles(model);
    await page.getByRole('button',{name:'Открыть обзор модели'}).click();
    const explorer=page.locator('.model-explorer');
    await explorer.locator('[data-query]').fill(driverB);
    await explorer.locator('[data-parameters] button').first().click();
    await explorer.getByRole('button',{name:'Добавить в стресс'}).click();
    assert.ok(await page.locator('.scenario-builder [data-field=source]').first().inputValue());
    await explorer.getByRole('button',{name:'Закрыть обзор'}).click();
    await page.getByRole('button',{name:'Открыть совместный стресс', exact:true}).click();
    const form = page.locator('.scenario-builder');
    const preview = form.getByRole('button',{name:'Проверить план'});
    const run = form.getByRole('button',{name:'Пересчитать в Excel'});
    await form.locator('[data-outputs] .scenario-row').first().waitFor();
    assert.equal(await run.isDisabled(),true);

    const periods = [periodA, periodB];
    for (const [i, period] of periods.entries()) {
      if (i) await form.getByRole('button',{name:'Добавить результат'}).click();
      const row = form.locator('[data-outputs] .scenario-row').nth(i);
      await row.locator('[data-field=metric]').selectOption(metric);
      await row.locator('[data-field=period]').selectOption(period);
    }

    const shocks = [[driverA,-10],[driverB,10]];
    const sources = new Set();
    for (const [result, period] of periods.entries()) {
      for (const [index, [query, pct]] of shocks.entries()) {
        const i = result * shocks.length + index;
        if (i) await form.getByRole('button',{name:'Добавить вход'}).click();
        const row = form.locator('[data-inputs] > .scenario-row').nth(i);
        await row.locator('[data-field=for-result]').selectOption(String(result));
        await row.locator('[data-field=query]').fill(query);
        await row.locator('[data-field=period]').fill(period);
        await row.locator('[data-field=pct]').fill(String(pct));
        await row.getByRole('button',{name:'Найти ячейки'}).click();
        await row.locator('[data-candidates] button').first().click();
        sources.add(await row.locator('[data-field=source]').inputValue());
      }
    }
    assert.equal(sources.size,4);

    async function prepare() {
      const prepared = page.waitForResponse(r=>r.url().includes('/excel-scenarios/') && r.url().endsWith('/prepare'));
      await preview.click();
      const response = await prepared;
      const data = await response.json();
      assert.equal(response.status(),200,JSON.stringify(data));
      await form.getByRole('heading',{name:'План Excel'}).waitFor();
      return data;
    }
    const before = await prepare();
    assert.equal(before.metrics.length,2);
    assert.equal(before.inputs.length,4);
    assert.deepEqual(new Set(before.inputs.map(i=>i.source)), sources);
    assert.deepEqual(before.metrics.map(m=>m.period),periods);
    for(const period of periods){
      assert.deepEqual(before.inputs.filter(i=>i.source_period===period).map(i=>i.requested_pct),[-10,10]);
    }
    assert.equal(await run.isEnabled(),true);
    const change = form.locator('[data-inputs] [data-field=pct]').first();
    await change.fill('-11');
    assert.equal(await run.isDisabled(),true);
    assert.equal(await form.getByRole('heading',{name:'План Excel'}).count(),0);
    await change.fill('-10');
    const reviewed = await prepare();
    assert.notEqual(reviewed.plan_id,before.plan_id);

    const execution = page.waitForResponse(r=>r.url().includes('/excel-scenarios/') && r.url().endsWith('/execute'));
    await run.click();
    const response = await execution;
    const data = await response.json();
    assert.equal(response.status(),200,JSON.stringify(data));
    assert.equal(data.plan_id,reviewed.plan_id);
    assert.equal(data.rollback_verified,true);
    assert.equal(data.individual.length,4);
    assert.equal(data.combined.metrics.length,2);
    await form.getByText('Откат подтверждён после каждого пересчёта.',{exact:true}).waitFor();
    assert.equal(await form.locator('[data-result] tbody tr').count(),6);
    assert.equal(await form.locator('[data-result] tbody tr.combined td').count(),2);
    assert.equal(await run.isDisabled(),true);
    console.log(JSON.stringify({status:'PASS',inputs:sources.size,outputs:data.metrics.length,
      individual:data.individual.length,rollback:data.rollback_verified}));
  } finally {
    await browser.close();
    assert.equal(digest(),original,'Исходная книга изменилась');
  }
}

main().catch(error=>{ console.error(error); process.exitCode=1; });
