"use strict";
(() => {
  const el = id => document.getElementById(id);
  let token = "", approval = null, active = null, dispatch = false, busy = false, connected = false;
  let generation = 0, poll = null;
  const errors = {unauthorized: "Token inválido ou sessão administrativa encerrada.", agent_disconnected: "O agente precisa estar conectado.", inspection_unsupported: "Este agente precisa da versão com suporte à inspeção OPC.", validation_required: "Valide novamente a lista completa antes de aplicar.", invalid_configuration: "Confira ProgID, tags únicas e intervalo inteiro entre 1000 e 60000 ms.", inspection_busy: "Há uma inspeção em andamento.", inspection_failed: "Não foi possível confirmar o servidor ou as tags OPC.", read_only_runtime: "Este runtime permite somente consulta.", inspection_timeout: "A inspeção excedeu o prazo.", timeout: "A inspeção excedeu o prazo."};
  const message = text => { el("message").textContent = text; };
  function invalidate() { approval = null; generation++; for (const row of el("tags").children) { const status = row.querySelector("span"); status.textContent = "Não validada"; status.className = ""; } updateButtons(); }
  function updateButtons() {
    const available = dispatch && connected && !!el("agents").value && !busy;
    for (const id of ["find-servers", "validate-all"]) el(id).disabled = !available;
    for (const row of el("tags").children) row.querySelector("button").disabled = !available;
    el("apply").disabled = !available || !approval;
  }
  async function api(path, payload) {
    const response = await fetch(path, {method: payload === undefined ? "GET" : "POST", cache: "no-store", headers: {Authorization: "Bearer " + token, ...(payload === undefined ? {} : {"Content-Type": "application/json"})}, ...(payload === undefined ? {} : {body: JSON.stringify(payload)})});
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) logout();
      throw new Error(errors[data.error] || "A operação não pôde ser concluída com segurança.");
    }
    return data;
  }
  const agentPath = action => "/api/v1/agents/" + encodeURIComponent(el("agents").value) + "/" + action;
  function plan(paths) {
    const tags = paths || [...el("tags").querySelectorAll("input")].map(input => input.value.trim());
    const rate = Number(el("interval").value), prog = el("prog-id").value.trim();
    if (!prog || !Number.isInteger(rate) || rate < 1000 || rate > 60000 || !tags.length || tags.length > 50 || tags.some(p => !p) || new Set(tags).size !== tags.length) throw new Error(errors.invalid_configuration);
    return {opc_prog_id: prog, update_rate_ms: rate, tags};
  }
  function addTag(path = "") {
    if (el("tags").children.length >= 50) { message("O limite é de 50 tags."); return; }
    invalidate();
    const row = document.createElement("tr"), input = document.createElement("input"), status = document.createElement("span");
    input.value = path; input.maxLength = 1024; input.setAttribute("aria-label", "Endereço OPC da tag"); input.placeholder = "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN";
    input.addEventListener("input", () => { status.textContent = "Não validada"; status.className = ""; invalidate(); });
    status.textContent = "Não validada";
    const validate = document.createElement("button"), remove = document.createElement("button");
    validate.textContent = "Validar"; remove.textContent = "Remover";
    validate.addEventListener("click", () => validateTags([input.value.trim()]));
    remove.addEventListener("click", () => { row.remove(); invalidate(); });
    for (const nodes of [[input], [status], [validate, remove]]) { const td = document.createElement("td"); td.append(...nodes); row.append(td); }
    el("tags").append(row); updateButtons();
  }
  async function validateTags(paths) {
    if (busy || !dispatch) return;
    try {
      const payload = plan(paths); invalidate(); const revision = generation;
      busy = true; updateButtons(); message("Validando em worker isolado…");
      const data = await api(agentPath("tag-validations"), payload);
      if (revision !== generation) { message("O plano mudou durante a validação. Valide novamente."); return; }
      for (const row of el("tags").children) {
        const result = data.results.find(r => r.opc_item_path === row.querySelector("input").value.trim());
        if (result) { const status = row.querySelector("span"); status.className = result.status; status.textContent = ({valid: "Válida", invalid: "Inválida", error: "Erro"})[result.status] + (result.hresult === null ? "" : " · HRESULT 0x" + result.hresult.toString(16).padStart(8, "0")); }
      }
      if (!paths && data.valid) approval = {id: data.validation_id, expires: Date.now() + data.expires_in_seconds * 1000};
      message(data.valid ? "Validação concluída. Confirme a aplicação quando o plano estiver pronto." : "Há tags inválidas ou com erro. A configuração ativa foi preservada.");
    } catch (error) { message(error.message); } finally { busy = false; updateButtons(); }
  }
  async function refresh() {
    if (!token || !el("agents").value) return;
    const agentId = el("agents").value;
    const [detail, config, history] = await Promise.all([api(agentPath("" ).slice(0, -1)), api(agentPath("active-config")), api("/api/v1/config-operations?agent_id=" + encodeURIComponent(agentId))]);
    if (el("agents").value !== agentId) return;
    const agent = detail.agent; connected = agent.state === "connected" && !!agent.current_session; updateButtons(); el("agent-state").replaceChildren();
    for (const [label, value] of [["Estado", agent.state], ["Sessão", agent.current_session?.session_id || "—"], ["Último heartbeat", agent.last_heartbeat || "—"]]) { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = label; dd.textContent = value; el("agent-state").append(dt, dd); }
    if (approval && (active?.version ?? null) !== (config.configuration?.version ?? null)) invalidate();
    active = config.configuration;
    el("active-config").textContent = active ? "Versão " + active.version + " · " + active.opc_prog_id + " · " + active.update_rate_ms + " ms\n" + active.tags.join("\n") : "Nenhuma configuração aplicada registrada.";
    el("load-active").disabled = !active;
    el("operations").replaceChildren();
    for (const operation of history.operations) { const tr = document.createElement("tr"); for (const value of [operation.version, operation.status, operation.requested_at, operation.completed_at || "—", operation.operation_id + (operation.error ? " · " + operation.error : "")]) { const td = document.createElement("td"); td.textContent = value; tr.append(td); } el("operations").append(tr); }
  }
  function logout() { token = ""; approval = null; generation++; active = null; dispatch = false; connected = false; clearInterval(poll); el("workspace").hidden = true; el("login").hidden = false; el("logout").hidden = true; el("agent-state").replaceChildren(); el("active-config").textContent = ""; el("operations").replaceChildren(); el("tags").replaceChildren(); el("agents").replaceChildren(); el("servers").replaceChildren(new Option("ProgID manual", "")); el("prog-id").value = ""; el("token").value = ""; }
  el("login-form").addEventListener("submit", async event => {
    event.preventDefault(); token = el("token").value; el("token").value = "";
    try { await api("/health"); const [list, capabilities] = await Promise.all([api("/api/v1/agents"), api("/api/v1/ui-capabilities")]); dispatch = capabilities.dispatch_available;
      el("agents").replaceChildren(...list.agents.map(a => new Option(a.agent_id, a.agent_id)));
      el("login").hidden = true; el("workspace").hidden = false; el("logout").hidden = false; addTag(); updateButtons(); await refresh();
      message(dispatch ? "Selecione o servidor OPC e configure as tags." : "Runtime somente leitura: validação e aplicação indisponíveis.");
      clearInterval(poll); poll = setInterval(() => { refresh().catch(e => message(e.message)); if (approval && Date.now() >= approval.expires) invalidate(); }, 5000);
    } catch (error) { logout(); message(error.message); }
  });
  el("logout").addEventListener("click", logout);
  el("refresh").addEventListener("click", () => refresh().catch(e => message(e.message)));
  el("agents").addEventListener("change", () => { connected = false; invalidate(); el("servers").replaceChildren(new Option("ProgID manual", "")); el("tags").replaceChildren(); el("prog-id").value = ""; addTag(); refresh().catch(e => message(e.message)); });
  el("prog-id").addEventListener("input", invalidate); el("interval").addEventListener("input", invalidate);
  el("servers").addEventListener("change", () => { el("prog-id").value = el("servers").value; invalidate(); });
  el("find-servers").addEventListener("click", async () => { try { busy = true; updateButtons(); message("Consultando servidores no agente…"); const revision = generation; const data = await api(agentPath("opc-servers")); if (revision !== generation) return; el("servers").replaceChildren(new Option("ProgID manual", ""), ...data.servers.map(p => new Option(p, p))); message(data.servers.length ? "Escolha um servidor ou informe um ProgID manual." : "Nenhum servidor anunciado. Informe o ProgID manual para validar."); } catch (error) { message(error.message); } finally { busy = false; updateButtons(); } });
  el("add-tag").addEventListener("click", () => addTag());
  el("validate-all").addEventListener("click", () => validateTags());
  el("load-active").addEventListener("click", () => { if (!active) return; invalidate(); el("prog-id").value = active.opc_prog_id; el("interval").value = active.update_rate_ms; el("tags").replaceChildren(); active.tags.forEach(addTag); });
  el("apply").addEventListener("click", async () => {
    try { if (!approval || Date.now() >= approval.expires) { invalidate(); throw new Error(errors.validation_required); }
      const payload = plan(); if (!window.confirm("Aplicar este plano de leitura de " + payload.tags.length + " tag(s) ao agente " + el("agents").value + "?")) return;
      busy = true; updateButtons(); const data = await api(agentPath("tag-config-operations"), {...payload, validation_id: approval.id, confirmed: true}); invalidate(); message("Operação " + data.operation_id + " · v" + data.version + " · " + data.status); await refresh();
    } catch (error) { message(error.message); } finally { busy = false; updateButtons(); }
  });
})();
