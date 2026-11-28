// Рабочее место инспектора. Поиск, пересчёт и чат используют общие инструменты приложения.
function showCreditReview(upload) {
  const sid = SID, view = workbookView;
  const current = () => sid === SID && view === workbookView;
  const endpoint = '/api/credit-review/' + sid;
  const panel = el(`<section class="credit-workbench" aria-label="Кредитная проверка">
    <div class="review-heading"><h2>Кредитная проверка</h2>
      <p class="review-context">${esc(upload.card.file)} · ${upload.card.sheets_total} листов · ${upload.engine === 'excel' ? 'Пересчёт в Excel' : 'Анализ файла'}</p></div>
    <nav aria-label="Этапы работы с моделью">
      <button type="button" class="btn" data-step="overview">Обзор</button>
      <button type="button" class="btn" data-step="review">Проверка</button>
      <button type="button" class="btn" data-step="scenarios">Сценарии</button>
      <button type="button" class="btn" data-step="conclusion">Вывод</button>
    </nav>
    <section data-stage="overview">
      <p data-review-loading role="status">Собираю обзор книги…</p>
      <div data-overview></div>
      <div data-passport></div>
    </section>
    <section data-stage="review" hidden>
      <h3>Что важно проверить</h3>
      <div data-covenant class="review-section"></div>
      <div data-reconciliation></div>
      <details class="review-section" data-model-checks>
        <summary>Отраслевые и модельные проверки</summary>
        <div data-review></div>
      </details>
    </section>
    <section data-stage="scenarios" hidden>
      <h3>Проверить сценарий</h3>
      <div class="review-section" data-scenario-start>
        <p>Выберите, какие допущения изменить и по каким показателям сравнить результат с базой.</p>
        <button type="button" class="btn review-primary" data-open-scenario>Настроить сценарий</button>
      </div>
      <div data-scenario-container></div>
      <button type="button" class="btn review-primary" data-go-conclusion hidden>Перейти к выводу</button>
    </section>
    <section data-stage="conclusion" hidden>
      <h3>Черновик заключения</h3>
      <div data-conclusion role="status"></div>
    </section>
  </section>`);
  $('#workbench').replaceChildren(panel);
  const find = selector => panel.querySelector(selector);
  const titles = {
    inactive_only: 'Найдена только неактивная строка', review: 'Нужно разобраться в допущении',
    source_detail_unavailable: 'Часть расчёта находится вне видимой логики книги',
    limited: 'Недостаточное покрытие карты', confirm_applicability: 'Сверьте применимость',
    found: 'Строки найдены, связь требует проверки', mapped: 'Есть связь по карте формул',
    not_applicable: 'Не применяется по подтверждённому источнику',
  };
  const priority = Object.keys(titles);
  let data, planId, stage, reviewMetric, request = 0, conclusionRequest = 0;

  panel.showStage = next => {
    if (stage !== next) $('#workspace').scrollTop = 0;
    stage = next;
    panel.querySelectorAll('[data-stage]').forEach(item => { item.hidden = item.dataset.stage !== next; });
    panel.querySelectorAll('[data-step]').forEach(button => {
      if (button.dataset.step === next) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
    });
    if (stage === 'conclusion') showConclusion();
  };
  panel.scenarioComplete = id => {
    planId = id;
    find('[data-go-conclusion]').hidden = false;
    refresh();
  };
  panel.querySelectorAll('[data-step]').forEach(button => {
    button.onclick = () => panel.showStage(button.dataset.step);
  });
  find('[data-open-scenario]').onclick = () => openScenarioBuilder();
  find('[data-go-conclusion]').onclick = () => panel.showStage('conclusion');
  panel.showStage('overview');
  refresh();

  async function refresh() {
    const ticket = ++request;
    try {
      const fresh = await workbookRequest(endpoint);
      if (current() && ticket === request) render(fresh);
    } catch (error) {
      if (current() && ticket === request) {
        const loading = find('[data-review-loading]');
        loading.hidden = false;
        loading.textContent = 'Не удалось прочитать обзор книги. ' + error.message;
        const retry = el('<button type="button" class="btn">Повторить</button>');
        retry.onclick = refresh;
        loading.append(' ', retry);
      }
    }
  }

  function render(fresh) {
    data = fresh;
    const next = [...fresh.checks].sort((a, b) => priority.indexOf(a.status) - priority.indexOf(b.status));
    find('[data-review-loading]').hidden = true;
    renderOverview();
    renderChecks(next);
    renderCovenant();
    renderReconciliation();
  }

  function numbers(rows, className) {
    return `<dl class="${className}">${rows.map(row => `<div class="review-number">
      <dt>${esc(row.name || row.label)}</dt>
      <dd><strong>${fmt(row.value)}${row.unit ? ' ' + esc(row.unit) : ''}</strong>
        <small>${esc(row.period)} · <code>${esc(row.ref)}</code></small>
        ${row.basis ? `<br><small>${row.basis === 'maximum' ? 'Максимум' : 'Минимум'} в прогнозе${row.binding_confirmed ? '' : ' · строка предложена'}</small>` : ''}</dd></div>`).join('')}</dl>`;
  }

  function comparison(metric) {
    return numbers(metric.difference.rows.map(row => ({...row, unit: metric.difference.unit})), 'review-evidence');
  }

  function renderOverview() {
    const {model, profiles, baseline, covenants, confirmed_covenant: saved} = data;
    const available = covenants.filter(item => item.difference);
    const first = saved ? null : available.find(item => item.id === 'dscr') || available[0];
    const included = profiles.filter(item => item.included);
    const industries = included.filter(item => item.group === 'industry');
    const ambiguousIndustry = industries.length > 1 && industries.some(item => item.status === 'suggested');
    const context = [
      model.forecast_start ? 'Прогноз с ' + model.forecast_start : 'Начало прогноза требует уточнения',
      ...included.filter(item => !ambiguousIndustry || item.group !== 'industry').map(item =>
        item.name + (item.status === 'suggested' ? ' (предположение)' : '')),
    ];
    const risks = [...data.readiness.blockers, ...data.readiness.warnings];
    const scenarios = data.scenario_sets.filter(item => item.verified && item.active.length);
    const headlineFigures = baseline.filter(row => !first?.difference.rows.some(source => source.ref === row.ref));
    const parameters = upload.manifest?.suggested_parameters || [];
    const period = first?.difference.period;
    const root = find('[data-overview]');
    root.innerHTML = `<p class="review-context">${context.map(esc).join(' · ')}
      ${ambiguousIndustry ? ' · <button type="button" class="btn review-text-link" data-choose-industry>Уточнить отрасль</button>' : ''}</p>
      <div class="review-focus" ${first ? 'data-difference' : ''}>
        ${first ? `<h3>У ${esc(first.name)} разные значения за ${esc(period)}${/^\d{4}$/.test(period) ? ' год' : ''}</h3>
          <p>Выбор строки может изменить вывод о соблюдении ковенанта. Сверьте, какое определение используется в договоре.</p>
          ${comparison(first)}<p class="review-source">Значения из загруженной книги.</p>`
          : saved ? `<h3>Условия ковенанта заданы</h3>
            <p>Проверьте значения в Excel или используйте подтверждённые ячейки для стресс-сценария.</p>`
          : `<h3>${covenants.length ? 'Сверьте условия ковенанта' : 'Уточните показатели модели'}</h3>
            <p>${covenants.length ? 'Укажите строку, порог и периоды из кредитного договора, чтобы оценить запас прочности.'
              : 'Откройте справочник, чтобы посмотреть найденные показатели, параметры и связи между листами.'}</p>`}
        <div class="review-controls">
          ${covenants.length ? `<button type="button" class="btn review-primary" data-go-review>${first ? 'Сверить строки ' + esc(first.name) : 'Перейти к проверке'}</button>` : ''}
          <button type="button" class="btn ${covenants.length ? '' : 'review-primary'}" data-model>Открыть справочник модели</button>
        </div>
      </div>
      ${headlineFigures.length ? `<section class="review-section" aria-label="Ключевые значения книги">
        <h3>Ключевые значения книги</h3>
        <p class="review-source">Значения из файла на момент загрузки. У каждой цифры указан период и адрес ячейки.</p>
        ${numbers(headlineFigures, 'review-metrics')}
      </section>` : ''}
      ${parameters.length ? `<details class="review-section"><summary>Параметры и связи между листами</summary>
        <p>Примеры параметров, от которых по формулам зависят показатели модели.</p>
        <ul>${parameters.map((parameter, i) => `<li><button type="button" class="btn" data-parameter-link="${i}">${esc(parameter.name)}</button>
          · <code>${esc(parameter.ref)}</code></li>`).join('')}</ul></details>` : ''}
      ${risks.length || scenarios.length ? `<details class="review-section"><summary>Допущения и доступность расчётов</summary>
        ${scenarios.length ? '<p>Проверенные активные наборы: ' + esc(scenarios.flatMap(x => x.active).join(', ')) + '</p>' : ''}
        ${risks.length ? '<ul>' + risks.map(text => '<li>' + esc(text) + '</li>').join('') + '</ul>' : ''}</details>` : ''}`;
    root.querySelector('[data-go-review]')?.addEventListener('click', () => {
      if (first && reviewMetric !== first.id) {
        reviewMetric = first.id;
        renderCovenant();
      }
      panel.showStage('review');
      const form = find('[data-covenant] details');
      if (form) {
        form.open = true;
        form.querySelector('summary').focus();
      }
    });
    root.querySelector('[data-model]').onclick = openModelExplorer;
    root.querySelector('[data-choose-industry]')?.addEventListener('click', () => {
      panel.showStage('review');
      find('[data-model-checks]').open = true;
      const profile = find('[data-review] details');
      profile.open = true;
      profile.querySelector('summary').focus();
    });
    root.querySelectorAll('[data-parameter-link]').forEach(button => {
      button.onclick = () => openModelExplorer(parameters[Number(button.dataset.parameterLink)].id);
    });
  }

  function renderChecks(checks) {
    const container = find('[data-review]');
    container.innerHTML = `<details><summary>Тип модели и отрасль</summary><form data-profile-form>
      ${[['model', 'Тип модели'], ['industry', 'Отрасль и бизнес-модель']].map(([group, label]) =>
        `<fieldset><legend>${label}</legend>${data.profiles.filter(item => item.group === group).map(item =>
          `<label><input type="checkbox" value="${esc(item.id)}" ${item.included ? 'checked' : ''}>
            ${esc(item.name)}${item.status === 'suggested' ? ' · предложено по книге' : ''}
            ${item.evidence.length ? '<small> · ' + esc(item.evidence.map(x => x.ref).join(', ')) + '</small>' : ''}</label>`).join('')}</fieldset>`).join('')}
      <div class="scenario-actions"><button type="submit" class="btn">Подтвердить профили</button>
        <button type="button" class="btn" data-auto>Определить по книге</button></div>
      <p role="status" data-profile-status></p></form></details>
      <div data-findings></div>`;
    const form = container.querySelector('form');
    const save = async ids => {
      const ticket = ++request;
      const status = form.querySelector('[data-profile-status]');
      try {
        const result = await workbookRequest(endpoint + '/profiles', {file_hash: data.model.file_hash, profiles: ids});
        if (current() && ticket === request) render(result);
      } catch (error) { if (current() && ticket === request) status.textContent = error.message; }
    };
    form.onsubmit = event => {
      event.preventDefault();
      save([...form.querySelectorAll('input:checked')].map(input => input.value));
    };
    form.querySelector('[data-auto]').onclick = () => save(null);
    const findings = container.querySelector('[data-findings]');
    for (const check of checks) {
      const item = el(`<details class="review-finding"><summary><b>${esc(check.title)}</b> · ${esc(titles[check.status])}</summary>
        <ul>${check.fields.map(field => `<li><b>${esc(field.name)}</b>
          ${field.evidence.length ? '<ul>' + field.evidence.map(row => `<li>${esc(row.label)} · <code>${esc(row.ref)}</code>
            ${!row.active ? ' · неактивный набор' : !row.forecast ? ' · прогнозный период не найден' : row.linked ? ' · есть путь по карте' : ''}
            ${row.measured.map(proof => `<br>Excel: <code>${esc(proof.source)}</code> → <code>${esc(proof.target)}</code>, ${proof.delta === 0 ? 'эффект не обнаружен в этом прогоне' : 'Δ ' + fmt(proof.delta)}`).join('')}</li>`).join('') + '</ul>'
            : ' · строка не найдена'}</li>`).join('')}</ul>
        ${check.source_gaps?.length ? `<ul>${check.source_gaps.map(gap => `<li><code>${esc(gap.ref)}</code> ·
          ${gap.kind === 'external' ? 'внешняя ссылка' : 'готовое число в прогнозе'}</li>`).join('')}</ul>` : ''}
        <p><a target="_blank" rel="noopener noreferrer" href="${esc(check.source.url)}">${esc(check.source.name)}</a></p>
        ${check.conditional ? `<form data-applicability>
          <label>Применимость <select name="decision"><option value="">Нужно уточнить</option>
            <option value="yes">Применяется</option><option value="no">Не применяется</option></select></label>
          <label>Основание <input type="text" name="source" maxlength="240" value="${esc(check.decision?.source || '')}"></label>
          <button type="submit" class="btn">Сохранить решение</button><p data-decision-status role="status"></p>
        </form>` : ''}
        <div class="scenario-actions" data-probe-actions></div></details>`);
      const applicability = item.querySelector('[data-applicability]');
      if (applicability) {
        applicability.elements.decision.value = check.decision ? (check.decision.applies ? 'yes' : 'no') : '';
        applicability.onsubmit = async event => {
          event.preventDefault();
          const choice = applicability.elements.decision.value;
          const ticket = ++request;
          try {
            const result = await workbookRequest(endpoint + '/checks/' + encodeURIComponent(check.id), {
              file_hash: data.model.file_hash, applies: choice === '' ? null : choice === 'yes',
              source: applicability.elements.source.value,
            });
            if (current() && request === ticket) render(result);
          } catch (error) {
            if (current() && request === ticket) applicability.querySelector('[data-decision-status]').textContent = error.message;
          }
        };
      }
      const actions = item.querySelector('[data-probe-actions]');
      if (check.status !== 'not_applicable' && check.targets.length && upload.engine === 'excel') {
        for (const field of check.fields) {
          const input = field.evidence.filter(row => row.active).flatMap(row => row.inputs)
            .find(cell => check.targets.some(target => target.periods.includes(cell.period)));
          if (!input) continue;
          const output = check.targets.find(target => target.periods.includes(input.period));
          const button = el('<button type="button" class="btn"></button>');
          button.textContent = 'Проверить влияние · ' + field.name;
          button.onclick = () => openScenarioBuilder({source: input.ref, period: input.period,
                                                      metric: output.id, resultPeriod: input.period});
          actions.append(button);
        }
      }
      findings.append(item);
    }
  }

  function renderCovenant() {
    const container = find('[data-covenant]');
    if (!data.covenants.length) {
      container.innerHTML = '<h3>Ковенант</h3><p>В паспорте пока нет показателя с доступными периодами.</p>';
      return;
    }
    const saved = data.confirmed_covenant;
    container.innerHTML = `<details open><summary><b>Условия ковенанта</b>${saved ? ' · подтверждены' : ''}</summary>
      <p class="review-context">Выберите показатель и строку, затем перенесите из договора порог и периоды проверки.</p>
      <form><div class="review-fields"><label>Показатель <select name="metric">${data.covenants.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('')}</select></label>
        <label>Строка из определения в договоре <select name="binding" required></select></label></div>
        <div data-binding-preview></div>
        <fieldset><legend>Периоды проверки</legend><div class="review-periods" data-periods></div></fieldset>
        <div class="review-fields"><label>Сравнение <select name="op"><option value=">=">Не ниже</option><option value="<=">Не выше</option></select></label>
        <label>Порог <input name="threshold" type="number" step="any" required></label></div>
        <label>Документ и пункт <input name="source" type="text" maxlength="240" placeholder="Кредитный договор, п. 5.2" required></label>
        <div class="scenario-actions"><button type="submit" class="btn review-primary">Подтвердить условия</button>
          <button type="button" class="btn" data-base ${saved && upload.engine === 'excel' ? '' : 'hidden'}>Проверить базу Excel</button>
          <button type="button" class="btn" data-seed ${saved && saved.targets.length <= 8 ? '' : 'hidden'}>Подготовить стресс по ковенанту</button></div>
        <div data-rule-status role="status"></div>
      </form></details>`;
    const form = container.querySelector('form'), fields = form.elements;
    const metrics = () => data.covenants.find(item => item.id === fields.metric.value);
    const row = () => fields.binding.value === '' ? null : metrics().bindings[Number(fields.binding.value)];
    const periodGroup = form.querySelector('[data-periods]');
    const message = form.querySelector('[data-rule-status]');
    const preview = form.querySelector('[data-binding-preview]');
    const showPeriods = periods => {
      periodGroup.innerHTML = (row()?.periods || []).map(key =>
        `<label><input type="checkbox" name="periods" value="${esc(key)}" ${periods.includes(key) ? 'checked' : ''}> ${esc(key)}</label>`).join('');
      if (!row()) periodGroup.textContent = 'Сначала выберите строку.';
      const metric = metrics();
      const samples = (row()?.preview || []).slice(0, 3).map(item => ({
        ...item, name: row().label || metric.name, unit: row().unit,
      }));
      preview.innerHTML = metric.difference ? `<p class="review-context">Строки за ${esc(metric.difference.period)} дают разные значения в загруженной книге.</p>${comparison(metric)}`
        : samples.length ? numbers(samples, 'review-metrics') : '';
    };
    const bindings = selection => {
      fields.binding.replaceChildren(new Option('Выберите строку', ''), ...metrics().bindings.map((item, i) =>
        new Option(item.ref + ' · ' + (item.label || metrics().name) + (item.unit ? ' · ' + item.unit : ''), String(i))));
      fields.binding.value = selection < 0 ? '' : String(selection);
      showPeriods(selection >= 0 && saved ? saved.periods : []);
    };
    const invalidate = () => {
      form.querySelector('[data-base]').hidden = true;
      form.querySelector('[data-seed]').hidden = true;
      message.replaceChildren();
    };
    if (saved) {
      fields.metric.value = saved.metric;
      fields.op.value = saved.op;
      fields.threshold.value = saved.threshold;
      fields.source.value = saved.source;
    } else if (reviewMetric || data.covenants.some(item => item.id === 'dscr')) {
      fields.metric.value = reviewMetric || 'dscr';
    }
    bindings(saved ? metrics().bindings.findIndex(item => item.sheet === saved.sheet && item.row === saved.row) : -1);
    fields.metric.onchange = () => bindings(-1);
    fields.binding.onchange = () => showPeriods([]);
    form.addEventListener('change', invalidate);
    form.addEventListener('input', invalidate);
    form.onsubmit = async event => {
      event.preventDefault();
      const periods = [...form.querySelectorAll('[name=periods]:checked')].map(input => input.value);
      if (!fields.binding.value || !periods.length) {
        message.textContent = 'Выберите строку и хотя бы один период.';
        return;
      }
      try {
        const rule = await workbookRequest(endpoint + '/covenant', {
          file_hash: data.model.file_hash, metric: metrics().id, sheet: row().sheet, row: row().row, periods,
          op: fields.op.value, threshold: Number(fields.threshold.value), source: fields.source.value,
        });
        if (!current() || !form.isConnected) return;
        data.confirmed_covenant = rule;
        renderOverview();
        container.querySelector('summary').textContent = 'Условия ковенанта · подтверждены';
        form.querySelector('[data-base]').hidden = upload.engine !== 'excel';
        form.querySelector('[data-seed]').hidden = rule.targets.length > 8;
        message.textContent = rule.targets.length > 8
          ? 'Условия подтверждены. Для одного стресс-сценария доступны до восьми результатов.'
          : 'Условия подтверждены для этой загрузки.';
      } catch (error) { if (current()) message.textContent = error.message; }
    };
    form.querySelector('[data-seed]').onclick = () => openScenarioBuilder({targets: data.confirmed_covenant.targets});
    form.querySelector('[data-base]').onclick = async () => {
      message.textContent = 'Читаю подтверждённые ячейки в Excel…';
      try {
        const result = await workbookRequest(endpoint + '/covenant');
        if (current()) message.innerHTML = covenantSummary(result);
      } catch (error) { if (current()) message.textContent = error.message; }
    };
  }

  function covenantSummary(result) {
    if (!result.rule) return esc(result.detail || 'Статус ковенанта требует подтверждения условий сделки.');
    const first = result.first_breach, minimum = result.minimum_headroom;
    return `<p><b>${first ? 'Первое нарушение · ' + esc(first.period) : 'Ковенант соблюдён в выбранных периодах'}</b></p>
      <p>Минимальный запас ${fmt(minimum.headroom)} · ${esc(minimum.period)} · <code>${esc(minimum.ref)}</code></p>
      <p class="kv">Условие ${esc(result.rule.op)} ${fmt(result.rule.threshold)} · ${esc(result.rule.source)}</p>`;
  }

  function renderReconciliation() {
    const container = find('[data-reconciliation]');
    if (!data.reconciliations?.length || upload.engine !== 'excel') return;
    container.innerHTML = `<details class="review-section"><summary><b>Сверить расчёт EBITDA или DSCR</b></summary>
      <form><label>Определение <select name="definition">${data.reconciliations.map(item =>
        `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('')}</select></label>
        <label>Период <input name="period" list="credit-reconciliation-periods" required placeholder="2027 или 2027Q1"></label>
        <datalist id="credit-reconciliation-periods"></datalist>
        <div data-reconciliation-refs></div>
        <label>Знак третьей статьи <select name="sign" required>
          <option value="">Выберите по модели</option><option value="1">+1 · брать значение как есть</option>
          <option value="-1">−1 · поменять знак значения</option></select></label>
        <label>Числовой допуск <input name="tolerance" type="number" step="any" min="0" value="0.01" required></label>
        <label>Основание определения <input name="source" maxlength="240" required></label>
        <label><input name="basis" type="checkbox" required> Проверены валюта, масштаб и периметр</label>
        <button type="submit" class="btn">Сверить по Excel</button><div data-reconciliation-result role="status"></div>
      </form></details>`;
    const form = container.querySelector('form'), fields = form.elements;
    const template = () => data.reconciliations.find(item => item.id === fields.definition.value);
    const refs = form.querySelector('[data-reconciliation-refs]');
    const result = form.querySelector('[data-reconciliation-result]');
    function fill() {
      const spec = template(), candidates = spec.periods[fields.period.value] || [];
      form.querySelector('datalist').replaceChildren(...Object.keys(spec.periods).map(key => new Option(key, key)));
      refs.innerHTML = spec.labels.map((label, i) =>
        `<label>${esc(label)} <input name="ref" value="${esc(candidates[i]?.ref || '')}" placeholder="Лист!A1" required></label>`).join('');
    }
    fields.definition.onchange = fill;
    fields.period.onchange = fill;
    form.oninput = () => { result.replaceChildren(); };
    fill();
    form.onsubmit = async event => {
      event.preventDefault();
      try {
        const report = await workbookRequest(endpoint + '/reconcile', {
          file_hash: data.model.file_hash, definition: fields.definition.value, period: fields.period.value,
          refs: [...refs.querySelectorAll('input')].map(input => input.value),
          sign: Number(fields.sign.value), tolerance: Number(fields.tolerance.value),
          source: fields.source.value, comparable_basis: fields.basis.checked,
        });
        if (current() && form.isConnected) {
          result.innerHTML = `<p><b>${report.status === 'matched' ? 'Сходится в пределах допуска' : 'Есть расхождение'}</b>
            · в модели ${fmt(report.actual)} · по компонентам ${fmt(report.calculated)} · разница ${fmt(report.difference)}</p>
            <ul>${report.evidence.map(item => `<li>${esc(item.label)} · <code>${esc(item.ref)}</code>
              · ${fmt(item.value)} ${esc(item.unit)}</li>`).join('')}</ul>`;
        }
      } catch (error) { if (current() && form.isConnected) result.textContent = error.message; }
    };
  }

  async function showConclusion() {
    const container = find('[data-conclusion]');
    container.textContent = 'Проверяю историю сценариев…';
    const ticket = ++conclusionRequest;
    const active = () => current() && stage === 'conclusion' && ticket === conclusionRequest;
    try {
      const history = await workbookRequest('/api/excel-scenarios/' + sid);
      if (!active()) return;
      if (!history.scenarios.some(item => item.plan_id === planId)) planId = history.scenarios[0]?.plan_id;
      if (!planId) {
        container.innerHTML = '<p>Вывод появится после проверенного пересчёта Excel.</p><button type="button" class="btn review-primary">Перейти к сценарию</button>';
        container.querySelector('button').onclick = () => panel.showStage('scenarios');
        return;
      }
      const result = await workbookRequest(endpoint + '/conclusion?plan_id=' + encodeURIComponent(planId));
      if (!active()) return;
      container.innerHTML = `${history.scenarios.length > 1 ? '<label>Проверенный сценарий <select data-history></select></label>' : ''}
        ${covenantSummary(result.covenant)}
        <details><summary>Результаты и подтверждение отката</summary>${scenarioOutcome(result.run)}</details>
        <div class="scenario-actions"><button type="button" class="btn" data-save>Скачать черновик</button>
          <button type="button" class="btn" data-word>Скачать Word</button></div>
        <p role="status" data-export-status></p>`;
      const picker = container.querySelector('[data-history]');
      if (picker) {
        for (const scenario of history.scenarios) {
          const timestamp = new Date(scenario.verified_at).toLocaleString('ru-RU');
          picker.add(new Option(timestamp + ' · ' + scenario.inputs.join(', '), scenario.plan_id));
        }
        picker.value = planId;
        picker.onchange = () => { planId = picker.value; showConclusion(); };
      }
      container.querySelector('[data-save]').onclick = () =>
        saveBlob(new Blob([result.markdown], {type: 'text/markdown;charset=utf-8'}), 'Кредитное заключение.md');
      container.querySelector('[data-word]').onclick = async () => {
        const filename = 'Кредитное заключение.docx';
        try {
          const response = await fetch('/api/docx', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({markdown: result.markdown, filename}),
          });
          if (!response.ok) throw new Error('Не удалось сформировать Word.');
          if (active()) saveBlob(await response.blob(), filename);
        } catch (error) { if (active()) container.querySelector('[data-export-status]').textContent = error.message; }
      };
    } catch (error) { if (active()) container.textContent = error.message; }
  }
}
