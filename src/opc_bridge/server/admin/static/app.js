"use strict";
(() => {
  const el = id => document.getElementById(id);

  // State
  let currentTab = "equipments";
  let equipments = [];
  let availableAgents = [];
  let selectedEquipment = null;
  let savedConfigs = [];
  let currentConfigId = null;
  let activeAgentConfig = null;
  let approval = null;
  let dispatch = false;
  let busy = false;
  let livePolling = null;
  let autoRefreshTimer = null;
  let generation = 0;
  let currentWriteTags = [];
  let writeGlobalEnabled = false;

  const errors = {
    unauthorized: "Acesso não autorizado ou sessão expirada no proxy.",
    agent_disconnected: "O agente precisa estar conectado.",
    inspection_unsupported: "Este agente precisa da versão com suporte à inspeção OPC.",
    validation_required: "Valide novamente a lista completa antes de aplicar.",
    invalid_configuration: "Confira nome, ProgID, tags únicas (máx 50) e intervalo entre 1000 e 60000 ms.",
    invalid_ip: "Endereço IP inválido. Informe um IPv4 ou IPv6 válido.",
    invalid_name: "Informe um nome válido (1 a 255 caracteres).",
    invalid_tags: "Informe entre 1 e 50 tags únicas e válidas.",
    invalid_interval: "Intervalo deve ser um inteiro entre 1000 e 60000 ms.",
    equipment_has_linked_configs: "O equipamento possui configurações vinculadas e requer confirmação para exclusão.",
    equipment_not_found: "Equipamento não encontrado.",
    inspection_busy: "Há uma inspeção em andamento.",
    inspection_failed: "Não foi possível confirmar o servidor ou as tags OPC.",
    read_only_runtime: "Este runtime permite somente consulta.",
    inspection_timeout: "A inspeção excedeu o prazo.",
    timeout: "A inspeção excedeu o prazo."
  };

  function message(text, type = "info") {
    const box = el("message");
    if (!text) {
      box.textContent = "";
      box.className = "";
      box.style.display = "none";
      return;
    }
    box.textContent = text;
    box.className = type === "error" ? "error-msg" : (type === "success" ? "success-msg" : "");
    box.style.display = "block";
  }

  async function api(path, payload, method = null) {
    const httpMethod = method || (payload === undefined ? "GET" : "POST");
    const options = {
      method: httpMethod,
      cache: "no-store",
      headers: payload === undefined ? {} : {"Content-Type": "application/json"}
    };
    if (payload !== undefined) {
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) {
      const err = new Error(errors[data.error] || data.error || "Operação não pôde ser concluída.");
      err.data = data;
      throw err;
    }
    return data;
  }

  // IP Regex validation helper (IPv4 and IPv6)
  function isValidIP(ip) {
    if (!ip || typeof ip !== "string") return false;
    const trimmed = ip.trim();
    const ipv4Regex = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
    const ipv6Regex = /^([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}$|^(([0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4})?::(([0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4})?$/;
    return ipv4Regex.test(trimmed) || ipv6Regex.test(trimmed);
  }

  // Formatting date/time for Último timestamp: dd/MM/yyyy HH:mm:ss.SSS
  function formatOpcTimestamp(ts) {
    if (!ts) return "—";
    if (typeof ts === "string" && ts.includes("/")) return ts;
    try {
      const d = new Date(ts);
      if (isNaN(d.getTime())) return String(ts);
      const pad = (n, l = 2) => String(n).padStart(l, "0");
      return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
    } catch (e) {
      return String(ts);
    }
  }

  function formatAge(ageMs) {
    if (ageMs === null || ageMs === undefined) return "—";
    if (ageMs < 1000) return `${ageMs} ms`;
    return `${(ageMs / 1000).toFixed(1)} s`;
  }

  // Tabs switching
  function switchTab(tab) {
    currentTab = tab;
    stopLive();
    const tabs = ["equipments", "opc", "write-opc"];
    tabs.forEach(t => {
      const btn = el(`tab-${t}`);
      const panel = el(`panel-${t}`);
      if (btn && panel) {
        const isActive = t === tab;
        btn.classList.toggle("active", isActive);
        btn.setAttribute("aria-selected", isActive ? "true" : "false");
        panel.hidden = !isActive;
      }
    });

    if (tab === "equipments") {
      loadEquipments().catch(e => message(e.message, "error"));
    } else if (tab === "opc") {
      populateEquipmentDropdown();
      if (el("opc-equipment-select").value) {
        onEquipmentSelected().catch(e => message(e.message, "error"));
      }
    } else if (tab === "write-opc") {
      populateWriteEquipmentDropdown();
      if (selectedEquipment && selectedEquipment.equipment_id) {
        el("write-equipment-select").value = selectedEquipment.equipment_id;
      }
      if (el("write-equipment-select").value) {
        loadWriteEquipment().catch(e => message(e.message, "error"));
      }
    }
  }

  // ==========================================
  // MODULE 1: EQUIPAMENTOS
  // ==========================================

  async function loadEquipments() {
    try {
      const data = await api("/api/v1/equipments");
      equipments = data.equipments || [];
      renderEquipmentsTable();
      populateEquipmentDropdown();
      populateWriteEquipmentDropdown();
    } catch (err) {
      message(err.message, "error");
    }
  }

  function renderEquipmentsTable() {
    const tbody = el("equipments-list");
    tbody.replaceChildren();

    if (!equipments.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 6;
      td.textContent = "Nenhum equipamento cadastrado. Clique em 'Adicionar equipamento' para começar.";
      td.className = "muted";
      tr.append(td);
      tbody.append(tr);
      return;
    }

    for (const eq of equipments) {
      const tr = document.createElement("tr");

      const tdName = document.createElement("td");
      tdName.textContent = eq.name;
      tdName.style.fontWeight = "600";

      const tdIp = document.createElement("td");
      tdIp.textContent = eq.ip_address;

      const tdAgent = document.createElement("td");
      tdAgent.textContent = eq.agent_id ? `${eq.agent_id} (${eq.agent_display_name || eq.agent_id})` : "—";

      const tdStatus = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = "badge " + (eq.agent_status || "unassociated");
      badge.textContent = eq.agent_status === "connected" ? "Conectado" : (eq.agent_status === "disconnected" ? "Desconectado" : "Não associado");
      tdStatus.append(badge);

      const tdConfigs = document.createElement("td");
      tdConfigs.textContent = `${eq.config_count || 0} configurada(s)`;

      const tdActions = document.createElement("td");
      const btnEdit = document.createElement("button");
      btnEdit.textContent = "Editar";
      btnEdit.type = "button";
      btnEdit.style.marginRight = "6px";
      btnEdit.addEventListener("click", () => showEditEquipmentForm(eq));

      const btnDelete = document.createElement("button");
      btnDelete.textContent = "Excluir";
      btnDelete.type = "button";
      btnDelete.className = "danger";
      btnDelete.addEventListener("click", () => deleteEquipment(eq));

      tdActions.append(btnEdit, btnDelete);
      tr.append(tdName, tdIp, tdAgent, tdStatus, tdConfigs, tdActions);
      tbody.append(tr);
    }
  }

  function showAddEquipmentForm() {
    el("equipment-form-title").textContent = "Adicionar equipamento";
    el("equipment-form-id").value = "";
    el("eq-name").value = "";
    el("eq-ip").value = "";
    populateAgentSelect("eq-agent", "");
    el("equipment-form-card").hidden = false;
    el("eq-name").focus();
  }

  function showEditEquipmentForm(eq) {
    el("equipment-form-title").textContent = "Editar equipamento";
    el("equipment-form-id").value = eq.equipment_id;
    el("eq-name").value = eq.name;
    el("eq-ip").value = eq.ip_address;
    populateAgentSelect("eq-agent", eq.agent_id || "");
    el("equipment-form-card").hidden = false;
    el("eq-name").focus();
  }

  function hideEquipmentForm() {
    el("equipment-form-card").hidden = true;
    el("equipment-form-id").value = "";
    el("eq-name").value = "";
    el("eq-ip").value = "";
  }

  async function saveEquipment() {
    const id = el("equipment-form-id").value;
    const name = el("eq-name").value.trim();
    const ip = el("eq-ip").value.trim();
    const agentId = el("eq-agent").value || null;

    if (!name) {
      message("Informe o nome do equipamento.", "error");
      return;
    }
    if (!isValidIP(ip)) {
      message("Informe um IP válido (IPv4 ou IPv6).", "error");
      return;
    }

    try {
      const payload = {name, ip_address: ip, agent_id: agentId};
      if (id) {
        await api(`/api/v1/equipments/${encodeURIComponent(id)}`, payload, "PUT");
        message("Equipamento atualizado com sucesso.", "success");
      } else {
        await api("/api/v1/equipments", payload, "POST");
        message("Equipamento adicionado com sucesso.", "success");
      }
      hideEquipmentForm();
      await loadEquipments();
    } catch (err) {
      message(err.message, "error");
    }
  }

  async function deleteEquipment(eq) {
    try {
      let confirmed = false;
      if (eq.config_count > 0) {
        const confData = await api(`/api/v1/opc-configs?equipment_id=${encodeURIComponent(eq.equipment_id)}`);
        const names = (confData.configs || []).map(c => `• ${c.name}`).join("\n");
        const msg = `ATENÇÃO: O equipamento '${eq.name}' possui ${eq.config_count} configuração(ões) OPC vinculada(s):\n\n${names}\n\nDeseja realmente excluir este equipamento e todas as configurações vinculadas?`;
        confirmed = window.confirm(msg);
      } else {
        confirmed = window.confirm(`Deseja realmente excluir o equipamento '${eq.name}'?`);
      }

      if (!confirmed) return;

      await api(`/api/v1/equipments/${encodeURIComponent(eq.equipment_id)}?confirmed=true`, undefined, "DELETE");
      message(`Equipamento '${eq.name}' excluído com sucesso.`, "success");
      if (selectedEquipment?.equipment_id === eq.equipment_id) {
        selectedEquipment = null;
        stopLive();
      }
      await loadEquipments();
    } catch (err) {
      message(err.message, "error");
    }
  }

  function populateAgentSelect(selectId, selectedAgentId) {
    const select = el(selectId);
    select.replaceChildren(new Option("Nenhum / Não associado", ""));
    for (const a of availableAgents) {
      const opt = new Option(`${a.agent_id} (${a.display_name || a.agent_id})`, a.agent_id);
      if (a.agent_id === selectedAgentId) opt.selected = true;
      select.append(opt);
    }
  }

  // ==========================================
  // MODULE 2: OPC
  // ==========================================

  function populateEquipmentDropdown() {
    const select = el("opc-equipment-select");
    const currentVal = select.value;
    select.replaceChildren(new Option("Selecione um equipamento...", ""));

    for (const eq of equipments) {
      const opt = new Option(`${eq.name} (${eq.ip_address})`, eq.equipment_id);
      if (eq.equipment_id === currentVal) opt.selected = true;
      select.append(opt);
    }
  }

  async function onEquipmentSelected() {
    stopLive();
    invalidateApproval();
    const eqId = el("opc-equipment-select").value;
    if (!eqId) {
      selectedEquipment = null;
      el("opc-agent-info").hidden = true;
      el("opc-agent-warning").hidden = true;
      el("saved-configs-list").replaceChildren();
      resetOpcForm();
      updateButtons();
      return;
    }

    selectedEquipment = equipments.find(e => e.equipment_id === eqId) || null;
    if (!selectedEquipment) return;

    // Show agent info
    el("opc-agent-info").hidden = false;
    el("meta-agent-id").textContent = selectedEquipment.agent_id || "Não associado";
    const statusText = selectedEquipment.agent_status === "connected" ? "Conectado" : (selectedEquipment.agent_status === "disconnected" ? "Desconectado" : "Não associado");
    el("meta-agent-status").textContent = statusText;
    el("meta-agent-status").className = "badge " + (selectedEquipment.agent_status || "unassociated");

    const isConnected = selectedEquipment.agent_status === "connected" && !!selectedEquipment.agent_id;
    el("opc-agent-warning").hidden = isConnected;

    // Fetch details for agent session if connected
    if (selectedEquipment.agent_id) {
      try {
        const agentDetail = await api(`/api/v1/agents/${encodeURIComponent(selectedEquipment.agent_id)}`);
        const agent = agentDetail.agent;
        el("meta-session-id").textContent = agent.current_session?.session_id || "—";
        el("meta-last-heartbeat").textContent = agent.last_heartbeat || "—";

        // Load active config and operations history
        await loadAgentActiveConfigAndHistory(selectedEquipment.agent_id);
      } catch (e) {
        el("meta-session-id").textContent = "—";
        el("meta-last-heartbeat").textContent = "—";
      }
    } else {
      el("meta-session-id").textContent = "—";
      el("meta-last-heartbeat").textContent = "—";
      el("active-config").textContent = "Equipamento não associado a um agente.";
      if (el("operations")) el("operations").replaceChildren();
    }

    // Load saved configs for this equipment
    await loadSavedConfigs(eqId);
    updateButtons();
  }

  async function loadAgentActiveConfigAndHistory(agentId) {
    try {
      const [cfgData, histData] = await Promise.all([
        api(`/api/v1/agents/${encodeURIComponent(agentId)}/active-config`),
        api(`/api/v1/config-operations?agent_id=${encodeURIComponent(agentId)}`)
      ]);

      activeAgentConfig = cfgData.configuration;
      if (activeAgentConfig) {
        el("active-config").textContent = `Versão ${activeAgentConfig.version} · ${activeAgentConfig.opc_prog_id} · ${activeAgentConfig.update_rate_ms} ms\n${activeAgentConfig.tags.join("\n")}`;
        el("btn-load-active").disabled = false;
      } else {
        el("active-config").textContent = "Nenhuma configuração aplicada registrada.";
        el("btn-load-active").disabled = true;
      }

      // History (if visual table exists)
      const tbody = el("operations");
      if (tbody) {
        tbody.replaceChildren();
        for (const op of (histData.operations || [])) {
          const tr = document.createElement("tr");
          for (const val of [op.version, op.status, op.requested_at, op.completed_at || "—", op.operation_id + (op.error ? ` · ${op.error}` : "")]) {
            const td = document.createElement("td");
            td.textContent = val;
            tr.append(td);
          }
          tbody.append(tr);
        }
      }
    } catch (err) {
      el("active-config").textContent = "Erro ao carregar dados do agente.";
    }
  }

  async function loadSavedConfigs(equipmentId) {
    try {
      const data = await api(`/api/v1/opc-configs?equipment_id=${encodeURIComponent(equipmentId)}`);
      savedConfigs = data.configs || [];
      renderSavedConfigsTable();
    } catch (err) {
      message(err.message, "error");
    }
  }

  function renderSavedConfigsTable() {
    const tbody = el("saved-configs-list");
    tbody.replaceChildren();

    if (!savedConfigs.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.textContent = "Nenhuma configuração salva para este equipamento. Preencha o formulário abaixo e clique em 'Salvar configuração'.";
      td.className = "muted";
      tr.append(td);
      tbody.append(tr);
      return;
    }

    for (const cfg of savedConfigs) {
      const tr = document.createElement("tr");

      const tdName = document.createElement("td");
      tdName.textContent = cfg.name;
      tdName.style.fontWeight = "600";

      const tdProg = document.createElement("td");
      tdProg.textContent = cfg.opc_prog_id;

      const tdInt = document.createElement("td");
      tdInt.textContent = `${cfg.interval_ms} ms`;

      const tdTags = document.createElement("td");
      tdTags.textContent = `${(cfg.tags || []).length} tag(s)`;

      const tdActions = document.createElement("td");
      const btnLoad = document.createElement("button");
      btnLoad.textContent = "Carregar";
      btnLoad.type = "button";
      btnLoad.style.marginRight = "6px";
      btnLoad.addEventListener("click", () => loadSavedConfigIntoForm(cfg));

      const btnDel = document.createElement("button");
      btnDel.textContent = "Excluir";
      btnDel.type = "button";
      btnDel.className = "danger";
      btnDel.addEventListener("click", () => deleteSavedConfig(cfg));

      tdActions.append(btnLoad, btnDel);
      tr.append(tdName, tdProg, tdInt, tdTags, tdActions);
      tbody.append(tr);
    }
  }

  function loadSavedConfigIntoForm(cfg) {
    stopLive();
    invalidateApproval();
    currentConfigId = cfg.config_id;
    el("config-name").value = cfg.name;
    el("prog-id").value = cfg.opc_prog_id;
    el("interval").value = cfg.interval_ms;
    el("servers").value = "";

    const tbody = el("tags");
    tbody.replaceChildren();
    if (cfg.tags && cfg.tags.length) {
      cfg.tags.forEach(t => addTag(t));
    } else {
      addTag();
    }
    updateTagCountBadge();
    updateButtons();
    message(`Configuração '${cfg.name}' carregada no formulário.`, "info");
  }

  async function deleteSavedConfig(cfg) {
    if (!window.confirm(`Deseja excluir a configuração salva '${cfg.name}'?`)) return;
    try {
      await api(`/api/v1/opc-configs/${encodeURIComponent(cfg.config_id)}`, undefined, "DELETE");
      message(`Configuração '${cfg.name}' excluída com sucesso.`, "success");
      if (currentConfigId === cfg.config_id) {
        currentConfigId = null;
      }
      if (selectedEquipment) {
        await loadSavedConfigs(selectedEquipment.equipment_id);
      }
    } catch (err) {
      message(err.message, "error");
    }
  }

  function resetOpcForm() {
    currentConfigId = null;
    el("config-name").value = "";
    el("prog-id").value = "";
    el("interval").value = "5000";
    el("servers").replaceChildren(new Option("Informar ProgID manualmente", ""));
    el("tags").replaceChildren();
    addTag();
    updateTagCountBadge();
  }

  function updateTagCountBadge() {
    const count = el("tags").children.length;
    el("tag-count-badge").textContent = `${count} / 50`;
  }

  function addTag(path = "") {
    const tbody = el("tags");
    if (tbody.children.length >= 50) {
      message("Limite de 50 tags atingido.", "error");
      return;
    }
    stopLive();
    invalidateApproval();

    const tr = document.createElement("tr");

    // 1. Tag path input
    const tdTag = document.createElement("td");
    const input = document.createElement("input");
    input.value = path;
    input.maxLength = 1024;
    input.placeholder = "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN";
    input.setAttribute("aria-label", "Endereço OPC da tag");
    input.addEventListener("input", () => {
      stopLive();
      invalidateApproval();
    });
    tdTag.append(input);

    // 2. Último valor
    const tdVal = document.createElement("td");
    tdVal.className = "col-val";
    tdVal.textContent = "—";

    // 3. Tipo
    const tdType = document.createElement("td");
    tdType.className = "col-type";
    tdType.textContent = "—";

    // 4. Qualidade
    const tdQual = document.createElement("td");
    tdQual.className = "col-qual";
    tdQual.textContent = "Não validada";

    // 5. Último timestamp (dd/MM/yyyy HH:mm:ss.SSS)
    const tdTs = document.createElement("td");
    tdTs.className = "col-ts";
    tdTs.textContent = "—";

    // 6. Idade
    const tdAge = document.createElement("td");
    tdAge.className = "col-age";
    tdAge.textContent = "—";

    // 7. Ação Excluir
    const tdAction = document.createElement("td");
    const btnDel = document.createElement("button");
    btnDel.textContent = "Excluir";
    btnDel.type = "button";
    btnDel.className = "danger";
    btnDel.addEventListener("click", () => {
      stopLive();
      invalidateApproval();
      tr.remove();
      updateTagCountBadge();
      updateButtons();
    });
    tdAction.append(btnDel);

    tr.append(tdTag, tdVal, tdType, tdQual, tdTs, tdAge, tdAction);
    tbody.append(tr);
    updateTagCountBadge();
    updateButtons();
  }

  function getTagsFromTable() {
    const inputs = [...el("tags").querySelectorAll("input")];
    return inputs.map(i => i.value.trim()).filter(v => !!v);
  }

  function clearValidationHighlights() {
    const banner = el("validation-errors-banner");
    if (banner) {
      banner.textContent = "";
      banner.hidden = true;
    }
    for (const row of el("tags").children) {
      row.classList.remove("row-error");
    }
  }

  function invalidateApproval() {
    approval = null;
    generation++;
    clearValidationHighlights();
    for (const row of el("tags").children) {
      const tdQual = row.querySelector(".col-qual");
      if (tdQual) {
        tdQual.textContent = "Não validada";
        tdQual.className = "col-qual";
      }
    }
    updateButtons();
  }

  function updateButtons() {
    const isConnected = selectedEquipment && selectedEquipment.agent_status === "connected" && !!selectedEquipment.agent_id;
    const canInspect = dispatch && isConnected && !busy;

    el("find-servers").disabled = !canInspect;
    el("validate-all").disabled = !canInspect;
    el("apply").disabled = !canInspect || !approval;

    const canLive = isConnected && !busy && ((activeAgentConfig && activeAgentConfig.tags.length > 0) || getTagsFromTable().length > 0);
    el("toggle-live").disabled = !canLive;
  }

  // Find OPC Servers
  async function findOpcServers() {
    if (!selectedEquipment?.agent_id || busy) return;
    try {
      busy = true;
      updateButtons();
      message("Buscando servidores OPC disponíveis no agente…", "info");
      const revision = generation;
      const data = await api(`/api/v1/agents/${encodeURIComponent(selectedEquipment.agent_id)}/opc-servers`);
      if (revision !== generation) return;

      const servers = data.servers || [];
      const select = el("servers");
      select.replaceChildren(new Option("Informar ProgID manualmente", ""));
      for (const s of servers) {
        select.append(new Option(s, s));
      }
      if (servers.length) {
        message(`${servers.length} servidor(es) OPC descoberto(s). Selecione ou informe o ProgID.`, "success");
      } else {
        message("Nenhum servidor OPC anunciado pelo agente. Informe o ProgID manualmente.", "info");
      }
    } catch (err) {
      message(err.message, "error");
    } finally {
      busy = false;
      updateButtons();
    }
  }

  // Read Now ("Ler agora")
  async function validateTagList(customPaths = null) {
    if (!selectedEquipment?.agent_id || busy) return;
    try {
      const tags = customPaths || getTagsFromTable();
      const prog = el("prog-id").value.trim();
      const rate = Number(el("interval").value);

      if (!prog || !Number.isInteger(rate) || rate < 1000 || rate > 60000 || !tags.length || tags.length > 50 || new Set(tags).size !== tags.length) {
        throw new Error(errors.invalid_configuration);
      }

      const isReusing = Boolean(
        activeAgentConfig &&
        activeAgentConfig.opc_prog_id &&
        activeAgentConfig.opc_prog_id.trim().toLowerCase() === prog.toLowerCase()
      );

      busy = true;
      updateButtons();
      const btnValidate = el("validate-all");
      if (btnValidate) btnValidate.textContent = isReusing ? "Lendo…" : "Conectando…";
      clearValidationHighlights();
      message(isReusing ? "Lendo valores atuais no servidor OPC…" : "Conectando ao servidor OPC…", "info");
      const revision = generation;

      const payload = {opc_prog_id: prog, update_rate_ms: rate, tags};
      const data = await api(`/api/v1/agents/${encodeURIComponent(selectedEquipment.agent_id)}/tag-validations`, payload);
      if (revision !== generation) return;

      const notFoundList = [];

      // Match results to table rows
      const rows = [...el("tags").children];
      rows.forEach((row, idx) => {
        const tagInput = row.querySelector("input");
        if (!tagInput) return;
        const path = tagInput.value.trim();
        const res = (data.results || []).find(r => r.opc_item_path === path);

        const tdVal = row.querySelector(".col-val");
        const tdType = row.querySelector(".col-type");
        const tdQual = row.querySelector(".col-qual");
        const tdTs = row.querySelector(".col-ts");
        const tdAge = row.querySelector(".col-age");

        if (res) {
          if (res.status === "valid") {
            row.classList.remove("row-error");
            if (tdVal) tdVal.textContent = res.value !== null && res.value !== undefined ? String(res.value) : "—";
            if (tdType) tdType.textContent = res.value_type || "—";
            if (tdQual) {
              tdQual.textContent = res.quality_text || (res.quality !== null && res.quality !== undefined ? String(res.quality) : "Válida");
              tdQual.className = "col-qual valid";
            }
            if (tdTs) tdTs.textContent = formatOpcTimestamp(res.opc_timestamp) || "—";
            if (tdAge) tdAge.textContent = "—";
          } else if (res.status === "invalid" || res.error === "Endereço OPC não encontrado.") {
            row.classList.add("row-error");
            if (tdVal) tdVal.textContent = "—";
            if (tdType) tdType.textContent = "—";
            if (tdQual) {
              tdQual.textContent = "Endereço OPC não encontrado.";
              tdQual.className = "col-qual error";
            }
            if (tdTs) tdTs.textContent = "—";
            if (tdAge) tdAge.textContent = "—";
            notFoundList.push({line: idx + 1, path});
          } else {
            // Other COM or OPC errors
            row.classList.remove("row-error");
            if (tdVal) tdVal.textContent = "—";
            if (tdType) tdType.textContent = "—";
            if (tdQual) {
              tdQual.textContent = res.error || (res.hresult ? `Erro (0x${res.hresult.toString(16).toUpperCase()})` : "Erro de leitura");
              tdQual.className = "col-qual error";
            }
            if (tdTs) tdTs.textContent = "—";
            if (tdAge) tdAge.textContent = "—";
          }
        }
      });

      // Consolidated alert banner
      const banner = el("validation-errors-banner");
      if (banner) {
        if (notFoundList.length > 0) {
          const count = notFoundList.length;
          const itemsText = notFoundList.map(item => `linha ${item.line} — ${item.path}`).join("; ");
          const label = count === 1 ? "1 endereço OPC não foi encontrado" : `${count} endereços OPC não foram encontrados`;
          banner.textContent = `${label}: ${itemsText}.`;
          banner.hidden = false;
        } else {
          banner.textContent = "";
          banner.hidden = true;
        }
      }

      if (data.valid) {
        approval = {id: data.validation_id, expires: Date.now() + (data.expires_in_seconds * 1000)};
        message("Leitura pontual concluída com sucesso. Todas as tags são válidas.", "success");
      } else {
        approval = null;
        if (notFoundList.length > 0) {
          message("A leitura identificou endereços OPC não encontrados. A configuração ativa do agente foi mantida intacta.", "error");
        } else {
          message("Há tags inválidas ou com erro no plano. A configuração ativa do agente foi mantida intacta.", "error");
        }
      }
    } catch (err) {
      message(err.message, "error");
    } finally {
      busy = false;
      const btnValidate = el("validate-all");
      if (btnValidate) btnValidate.textContent = "Ler agora";
      updateButtons();
    }
  }

  // Save Configuration (Administrative persistence only, NO CONFIG_PUSH)
  async function saveConfig() {
    if (!selectedEquipment) {
      message("Selecione um equipamento cadastrado.", "error");
      return;
    }
    const name = el("config-name").value.trim();
    const prog = el("prog-id").value.trim();
    const rate = Number(el("interval").value);
    const tags = getTagsFromTable();

    if (!name) {
      message("Informe um nome para a configuração OPC.", "error");
      return;
    }
    if (!prog) {
      message("Informe o ProgID do servidor OPC.", "error");
      return;
    }
    if (!Number.isInteger(rate) || rate < 1000 || rate > 60000) {
      message("Intervalo deve ser entre 1000 e 60000 ms.", "error");
      return;
    }
    if (!tags.length || tags.length > 50 || new Set(tags).size !== tags.length) {
      message("A configuração deve ter entre 1 e 50 tags únicas.", "error");
      return;
    }

    try {
      const payload = {
        name,
        equipment_id: selectedEquipment.equipment_id,
        opc_prog_id: prog,
        interval_ms: rate,
        tags,
        agent_id: selectedEquipment.agent_id || null
      };

      if (currentConfigId) {
        await api(`/api/v1/opc-configs/${encodeURIComponent(currentConfigId)}`, payload, "PUT");
        message(`Configuração '${name}' atualizada com sucesso no banco.`, "success");
      } else {
        const res = await api("/api/v1/opc-configs", payload, "POST");
        currentConfigId = res.config?.config_id || null;
        message(`Configuração '${name}' salva com sucesso no banco (sem alteração da coleta ativa).`, "success");
      }

      await loadSavedConfigs(selectedEquipment.equipment_id);
    } catch (err) {
      message(err.message, "error");
    }
  }

  // Apply to Agent (Explicit separate action with confirmation and CONFIG_PUSH)
  async function applyToAgent() {
    if (!selectedEquipment?.agent_id) {
      message("Equipamento não associado a um agente.", "error");
      return;
    }
    if (!approval || Date.now() >= approval.expires) {
      invalidateApproval();
      message("É obrigatório validar a lista com sucesso antes de aplicar no agente.", "error");
      return;
    }

    const tags = getTagsFromTable();
    const prog = el("prog-id").value.trim();
    const rate = Number(el("interval").value);

    const warnMsg = `ATENÇÃO: Aplicar este plano de leitura de ${tags.length} tag(s) substituirá a configuração ativa de leitura do agente '${selectedEquipment.agent_id}' no equipamento '${selectedEquipment.name}'. Deseja prosseguir?`;
    if (!window.confirm(warnMsg)) return;

    try {
      busy = true;
      updateButtons();
      message("Aplicando configuração no agente…", "info");

      const payload = {
        opc_prog_id: prog,
        update_rate_ms: rate,
        tags,
        validation_id: approval.id,
        confirmed: true
      };

      const res = await api(`/api/v1/agents/${encodeURIComponent(selectedEquipment.agent_id)}/tag-config-operations`, payload);
      stopLive();
      invalidateApproval();
      message(`Configuração aplicada no agente com sucesso! Operação: ${res.operation_id} · Versão: ${res.version} · ${res.status}`, "success");
      await loadAgentActiveConfigAndHistory(selectedEquipment.agent_id);
    } catch (err) {
      message(err.message, "error");
    } finally {
      busy = false;
      updateButtons();
    }
  }

  // ==========================================
  // LIVE MONITORING (Valores ao vivo)
  // ==========================================

  function startLive() {
    if (!selectedEquipment?.agent_id || busy) return;
    const btn = el("toggle-live");
    btn.textContent = "Parar valores ao vivo";
    btn.className = "primary";
    fetchLiveValues();
    if (livePolling) clearInterval(livePolling);
    livePolling = setInterval(fetchLiveValues, 1000);
  }

  function stopLive() {
    if (livePolling) {
      clearInterval(livePolling);
      livePolling = null;
    }
    const btn = el("toggle-live");
    if (btn) {
      btn.textContent = "Iniciar valores ao vivo";
      btn.className = "";
    }
  }

  function toggleLive() {
    if (livePolling) {
      stopLive();
    } else {
      startLive();
    }
  }

  async function fetchLiveValues() {
    if (!livePolling || !selectedEquipment?.agent_id) return;
    try {
      const data = await api(`/api/v1/agents/${encodeURIComponent(selectedEquipment.agent_id)}/live-values`);
      if (!livePolling) return;

      if (!data.connected || data.status === "agent_disconnected") {
        stopLive();
        message("Agente desconectado. Acompanhamento ao vivo interrompido automaticamente.", "error");
        return;
      }

      renderLiveTable(data.items || []);
    } catch (err) {
      if (!livePolling) return;
      message(err.message, "error");
    }
  }

  function renderLiveTable(items) {
    const itemsMap = new Map();
    for (const it of items) {
      itemsMap.set(it.opc_item_path, it);
    }

    const rows = el("tags").children;
    for (const row of rows) {
      const input = row.querySelector("input");
      if (!input) continue;
      const tagPath = input.value.trim();
      const it = itemsMap.get(tagPath);

      const tdVal = row.querySelector(".col-val");
      const tdType = row.querySelector(".col-type");
      const tdQual = row.querySelector(".col-qual");
      const tdTs = row.querySelector(".col-ts");
      const tdAge = row.querySelector(".col-age");

      if (!it || !it.available || it.status === "waiting_first_read") {
        if (tdVal) tdVal.textContent = "—";
        if (tdType) tdType.textContent = "—";
        if (tdQual) {
          tdQual.textContent = "Aguardando";
          tdQual.className = "col-qual badge pending";
        }
        if (tdTs) tdTs.textContent = "—";
        if (tdAge) tdAge.textContent = "—";
        continue;
      }

      // Value formatting
      if (tdVal) {
        if (it.value === null || it.value === undefined) {
          tdVal.textContent = "—";
        } else if (typeof it.value === "boolean") {
          tdVal.textContent = it.value ? "true" : "false";
        } else {
          tdVal.textContent = String(it.value);
        }
      }

      if (tdType) tdType.textContent = it.value_type || "—";

      // Quality and Status
      if (tdQual) {
        if (it.stale || it.status === "stale") {
          tdQual.textContent = "Desatualizado";
          tdQual.className = "col-qual badge stale";
        } else if (it.error || it.status === "error" || (it.quality !== null && it.quality < 192)) {
          tdQual.textContent = it.error ? `Erro: ${it.error}` : `Bad (${it.quality})`;
          tdQual.className = "col-qual badge error";
        } else {
          tdQual.textContent = it.quality_text ? `${it.quality_text} (${it.quality})` : "Good";
          tdQual.className = "col-qual badge good";
        }
      }

      // Último timestamp: dd/MM/yyyy HH:mm:ss.SSS
      if (tdTs) tdTs.textContent = formatOpcTimestamp(it.opc_timestamp);

      // Idade
      if (tdAge) tdAge.textContent = formatAge(it.age_ms);
    }
  }

  // ==========================================
  // MODULE 3: OPERAÇÃO OPC (ESCRITA CONTROLADA)
  // ==========================================

  function populateWriteEquipmentDropdown() {
    const sel = el("write-equipment-select");
    if (!sel) return;
    const currentVal = sel.value;
    sel.replaceChildren(new Option("Selecione um equipamento...", ""));
    equipments.forEach(eq => {
      sel.append(new Option(`${eq.name} (${eq.ip_address})`, eq.equipment_id));
    });
    if (currentVal && equipments.some(e => e.equipment_id === currentVal)) {
      sel.value = currentVal;
    } else if (selectedEquipment && equipments.some(e => e.equipment_id === selectedEquipment.equipment_id)) {
      sel.value = selectedEquipment.equipment_id;
    }
  }

  async function loadWriteEquipment() {
    const eqId = el("write-equipment-select").value;
    const tbody = el("write-spreadsheet-body");
    const banner = el("write-kill-switch-banner");
    const reviewBtn = el("btn-review-write");

    if (!eqId) {
      el("write-agent-info").hidden = true;
      banner.className = "banner warning";
      banner.textContent = "Selecione um equipamento para verificar as variáveis e permissões de escrita.";
      tbody.replaceChildren();
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 8;
      td.className = "muted";
      td.textContent = "Selecione um equipamento para carregar as tags operacionais.";
      tr.append(td);
      tbody.append(tr);
      if (reviewBtn) reviewBtn.disabled = true;
      renderWriteAuditTable([]);
      return;
    }

    try {
      const data = await api(`/api/v1/write-operation/tags?equipment_id=${encodeURIComponent(eqId)}`);
      writeGlobalEnabled = !!data.writes_enabled_globally;
      currentWriteTags = data.tags || [];

      // Update Kill Switch Banner
      if (writeGlobalEnabled) {
        banner.className = "banner info";
        banner.textContent = "Chave de escrita ATIVA no servidor (OPC_BRIDGE_ENABLE_WRITES=true). Gravação permitida apenas para tags com write_enabled=true.";
      } else {
        banner.className = "banner warning";
        banner.textContent = "Chave geral de escrita DESLIGADA (OPC_BRIDGE_ENABLE_WRITES=false). Nenhuma operação de escrita será aceita pelo servidor.";
      }

      // Metadata Grid
      el("write-agent-info").hidden = false;
      const eq = data.equipment || {};
      el("write-meta-agent-id").textContent = eq.agent_id || "Não associado";
      el("write-meta-agent-status").textContent = eq.agent_status === "connected" ? "Conectado" : (eq.agent_status === "disconnected" ? "Desconectado" : "Não associado");
      el("write-meta-prog-id").textContent = data.opc_prog_id || "—";
      const writableCount = currentWriteTags.filter(t => t.write_enabled).length;
      el("write-meta-permission").textContent = `${writableCount} de ${currentWriteTags.length} tag(s) autorizada(s) para escrita`;

      renderWriteSpreadsheet();
      await loadWriteAuditEvents(eqId);
    } catch (err) {
      message(err.message, "error");
    }
  }

  function renderWriteSpreadsheet() {
    const tbody = el("write-spreadsheet-body");
    tbody.replaceChildren();

    if (!currentWriteTags.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 8;
      td.className = "muted";
      td.textContent = "Nenhuma tag cadastrada nas configurações deste equipamento.";
      tr.append(td);
      tbody.append(tr);
      updateWriteReviewButton();
      return;
    }

    currentWriteTags.forEach((tag, idx) => {
      const tr = document.createElement("tr");
      tr.dataset.tag = tag.opc_item_path;

      // 1. Tag
      const tdTag = document.createElement("td");
      tdTag.textContent = tag.opc_item_path;
      tdTag.style.fontWeight = "600";
      tdTag.style.wordBreak = "break-all";

      // 2. Descrição
      const tdDesc = document.createElement("td");
      tdDesc.textContent = tag.description || "—";

      // 3. Tipo
      const tdType = document.createElement("td");
      const typeBadge = document.createElement("span");
      typeBadge.className = "badge";
      typeBadge.textContent = tag.data_type || "float";
      tdType.append(typeBadge);

      // 4. Limites
      const tdLimits = document.createElement("td");
      if (tag.allowed_values && Array.isArray(tag.allowed_values) && tag.allowed_values.length > 0) {
        tdLimits.textContent = `[${tag.allowed_values.join(", ")}]`;
      } else if (tag.min_value !== null || tag.max_value !== null) {
        tdLimits.textContent = `[${tag.min_value ?? "-∞"} .. ${tag.max_value ?? "+∞"}]`;
      } else {
        tdLimits.textContent = "—";
      }

      // 5. Valor atual
      const tdCurrent = document.createElement("td");
      tdCurrent.className = "col-val";
      tdCurrent.textContent = tag.current_value !== null && tag.current_value !== undefined ? String(tag.current_value) : "—";
      if (tag.quality !== null && tag.quality !== undefined) {
        const qBadge = document.createElement("span");
        qBadge.style.marginLeft = "6px";
        qBadge.className = "badge " + (tag.quality >= 192 ? "good" : "error");
        qBadge.textContent = tag.quality >= 192 ? "Good" : `Bad (${tag.quality})`;
        tdCurrent.append(qBadge);
      }

      // 6. Novo valor (Input Spreadsheet)
      const tdNew = document.createElement("td");
      const input = document.createElement("input");
      input.className = "write-cell-input";
      input.dataset.tag = tag.opc_item_path;
      input.dataset.index = String(idx);

      if (!tag.write_enabled) {
        input.disabled = true;
        input.placeholder = "Somente leitura";
        input.title = "write_enabled: false na configuração";
      } else if (!writeGlobalEnabled) {
        input.disabled = true;
        input.placeholder = "Chave geral OFF";
        input.title = "OPC_BRIDGE_ENABLE_WRITES=false";
      } else {
        input.placeholder = "Novo valor...";
        input.setAttribute("aria-label", `Novo valor para ${tag.opc_item_path}`);

        // Event: live validation
        input.addEventListener("input", () => {
          validateWriteCell(input);
          updateWriteReviewButton();
        });

        // Event: spreadsheet keyboard navigation (Enter, Shift+Enter, ArrowUp, ArrowDown)
        input.addEventListener("keydown", e => {
          const allInputs = [...tbody.querySelectorAll(".write-cell-input:not([disabled])")];
          const curPos = allInputs.indexOf(input);
          if (curPos === -1) return;

          if ((e.key === "Enter" && !e.shiftKey) || e.key === "ArrowDown") {
            e.preventDefault();
            if (curPos < allInputs.length - 1) allInputs[curPos + 1].focus();
          } else if ((e.key === "Enter" && e.shiftKey) || e.key === "ArrowUp") {
            e.preventDefault();
            if (curPos > 0) allInputs[curPos - 1].focus();
          }
        });

        // Event: spreadsheet multi-line paste (Excel copy-paste support)
        input.addEventListener("paste", e => {
          const pastedText = (e.clipboardData || window.clipboardData)?.getData("text") || "";
          if (pastedText.includes("\n") || pastedText.includes("\r")) {
            e.preventDefault();
            const lines = pastedText.split(/\r?\n/).map(s => s.trim()).filter(s => s.length > 0);
            const allInputs = [...tbody.querySelectorAll(".write-cell-input:not([disabled])")];
            const startPos = allInputs.indexOf(input);
            if (startPos !== -1) {
              lines.forEach((lineVal, offset) => {
                const targetInput = allInputs[startPos + offset];
                if (targetInput) {
                  targetInput.value = lineVal;
                  validateWriteCell(targetInput);
                }
              });
              updateWriteReviewButton();
            }
          }
        });
      }
      tdNew.append(input);

      // 7. Situação / Validação
      const tdStatus = document.createElement("td");
      const statusBadge = document.createElement("span");
      statusBadge.className = "badge status-badge";
      if (!tag.write_enabled) {
        statusBadge.classList.add("readonly");
        statusBadge.textContent = "Somente leitura";
      } else if (!writeGlobalEnabled) {
        statusBadge.classList.add("readonly");
        statusBadge.textContent = "Bloqueado (chave geral)";
      } else {
        statusBadge.classList.add("pending");
        statusBadge.textContent = "Em espera";
      }
      tdStatus.append(statusBadge);

      // 8. Timestamp
      const tdTs = document.createElement("td");
      tdTs.className = "col-ts";
      tdTs.textContent = formatOpcTimestamp(tag.timestamp);

      tr.append(tdTag, tdDesc, tdType, tdLimits, tdCurrent, tdNew, tdStatus, tdTs);
      tbody.append(tr);
    });

    updateWriteReviewButton();
  }

  function validateWriteCell(input) {
    const rawVal = input.value.trim();
    const tagPath = input.dataset.tag;
    const tagSpec = currentWriteTags.find(t => t.opc_item_path === tagPath);
    const row = input.closest("tr");
    const statusBadge = row?.querySelector(".status-badge");

    if (!rawVal) {
      input.classList.remove("valid", "invalid");
      if (statusBadge) {
        statusBadge.className = "badge status-badge pending";
        statusBadge.textContent = "Em espera";
      }
      return true;
    }

    if (!tagSpec) return false;

    let isValid = true;
    let errMsg = "";
    const dataType = (tagSpec.data_type || "float").toLowerCase();

    // Type validation
    if (dataType === "boolean") {
      const boolLow = rawVal.toLowerCase();
      if (!["true", "false", "1", "0", "sim", "nao", "não"].includes(boolLow)) {
        isValid = false;
        errMsg = "Inválido (esperado booleano)";
      }
    } else if (dataType === "integer") {
      if (!/^-?\d+$/.test(rawVal)) {
        isValid = false;
        errMsg = "Inválido (esperado inteiro)";
      }
    } else if (dataType === "float") {
      if (!/^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(rawVal) && isNaN(Number(rawVal))) {
        isValid = false;
        errMsg = "Inválido (esperado número)";
      }
    }

    // Bounds validation
    if (isValid && (dataType === "integer" || dataType === "float")) {
      const num = Number(rawVal);
      if (tagSpec.min_value !== null && tagSpec.min_value !== undefined && num < Number(tagSpec.min_value)) {
        isValid = false;
        errMsg = `Abaixo do mín (${tagSpec.min_value})`;
      } else if (tagSpec.max_value !== null && tagSpec.max_value !== undefined && num > Number(tagSpec.max_value)) {
        isValid = false;
        errMsg = `Acima do máx (${tagSpec.max_value})`;
      }
    }

    // Allowed values validation
    if (isValid && Array.isArray(tagSpec.allowed_values) && tagSpec.allowed_values.length > 0) {
      const allowedStr = tagSpec.allowed_values.map(String);
      if (!allowedStr.includes(rawVal)) {
        isValid = false;
        errMsg = "Valor não permitido";
      }
    }

    if (isValid) {
      input.classList.remove("invalid");
      input.classList.add("valid");
      if (statusBadge) {
        statusBadge.className = "badge status-badge applied";
        statusBadge.textContent = "Válido";
      }
      return true;
    } else {
      input.classList.remove("valid");
      input.classList.add("invalid");
      if (statusBadge) {
        statusBadge.className = "badge status-badge rejected";
        statusBadge.textContent = errMsg;
      }
      return false;
    }
  }

  function updateWriteReviewButton() {
    const btn = el("btn-review-write");
    if (!btn) return;
    if (!writeGlobalEnabled) {
      btn.disabled = true;
      return;
    }
    const inputs = [...el("write-spreadsheet-body").querySelectorAll(".write-cell-input:not([disabled])")];
    const filled = inputs.filter(i => i.value.trim().length > 0);
    const hasInvalid = filled.some(i => i.classList.contains("invalid"));
    btn.disabled = filled.length === 0 || hasInvalid;
  }

  function clearWriteValues() {
    const inputs = [...el("write-spreadsheet-body").querySelectorAll(".write-cell-input:not([disabled])")];
    inputs.forEach(input => {
      input.value = "";
      input.classList.remove("valid", "invalid");
      const row = input.closest("tr");
      const statusBadge = row?.querySelector(".status-badge");
      if (statusBadge) {
        statusBadge.className = "badge status-badge pending";
        statusBadge.textContent = "Em espera";
      }
    });
    updateWriteReviewButton();
    message("Valores da planilha limpos.", "info");
  }

  async function reviewWriteOperation() {
    const eqId = el("write-equipment-select").value;
    if (!eqId) return;

    const inputs = [...el("write-spreadsheet-body").querySelectorAll(".write-cell-input:not([disabled])")];
    const filled = inputs.filter(i => i.value.trim().length > 0);
    if (!filled.length) {
      message("Nenhum novo valor foi preenchido para escrita.", "error");
      return;
    }

    // Verify all cells
    let allValid = true;
    filled.forEach(input => {
      if (!validateWriteCell(input)) allValid = false;
    });
    if (!allValid) {
      message("Corrija os valores destacados em vermelho antes de prosseguir.", "error");
      updateWriteReviewButton();
      return;
    }

    const itemsToValidate = filled.map(i => ({
      tag: i.dataset.tag,
      value: i.value.trim()
    }));

    try {
      const valRes = await api("/api/v1/write-operation/validate", {
        equipment_id: eqId,
        items: itemsToValidate
      });

      if (!valRes.valid) {
        valRes.items.forEach(resItem => {
          if (!resItem.valid) {
            const input = inputs.find(i => i.dataset.tag === resItem.tag);
            if (input) {
              input.classList.remove("valid");
              input.classList.add("invalid");
              const row = input.closest("tr");
              const sb = row?.querySelector(".status-badge");
              if (sb) {
                sb.className = "badge status-badge rejected";
                sb.textContent = resItem.error || "Rejeitado na validação";
              }
            }
          }
        });
        message("Validação rejeitada pelo servidor. Verifique os valores informados.", "error");
        updateWriteReviewButton();
        return;
      }

      // Populate Confirmation Modal
      const modal = el("modal-write-confirm");
      const selOpt = el("write-equipment-select").selectedOptions[0];
      el("modal-eq-name").textContent = selOpt ? selOpt.textContent : eqId;
      el("modal-agent-id").textContent = el("write-meta-agent-id").textContent || "—";
      const operatorUser = el("write-operator-user").value.trim() || "operador";
      el("modal-user-id").textContent = operatorUser;

      const summaryBody = el("modal-write-summary-body");
      summaryBody.replaceChildren();

      filled.forEach(input => {
        const tagSpec = currentWriteTags.find(t => t.opc_item_path === input.dataset.tag);
        const tr = document.createElement("tr");

        const tdTag = document.createElement("td");
        tdTag.textContent = input.dataset.tag;
        tdTag.style.fontWeight = "600";

        const tdType = document.createElement("td");
        tdType.textContent = tagSpec?.data_type || "—";

        const tdOld = document.createElement("td");
        tdOld.textContent = tagSpec?.current_value !== null && tagSpec?.current_value !== undefined ? String(tagSpec.current_value) : "—";

        const tdNew = document.createElement("td");
        tdNew.textContent = input.value.trim();
        tdNew.style.fontWeight = "bold";
        tdNew.style.color = "var(--brand-blue)";

        tr.append(tdTag, tdType, tdOld, tdNew);
        summaryBody.append(tr);
      });

      modal.hidden = false;
    } catch (err) {
      message(err.message, "error");
    }
  }

  async function confirmAndExecuteWrite() {
    const eqId = el("write-equipment-select").value;
    const modal = el("modal-write-confirm");
    const confirmBtn = el("btn-confirm-execute-write");
    const inputs = [...el("write-spreadsheet-body").querySelectorAll(".write-cell-input:not([disabled])")];
    const filled = inputs.filter(i => i.value.trim().length > 0);

    const items = filled.map(i => ({
      tag: i.dataset.tag,
      value: i.value.trim()
    }));
    const username = el("write-operator-user").value.trim() || "operador";

    confirmBtn.disabled = true;
    confirmBtn.textContent = "Gravando...";

    try {
      const res = await api("/api/v1/write-operation/execute", {
        equipment_id: eqId,
        items,
        confirmed: true,
        username
      });

      modal.hidden = true;
      confirmBtn.disabled = false;
      confirmBtn.textContent = "Confirmar e Gravar";

      // Process per-tag results
      const resultsMap = new Map((res.results || []).map(r => [r.tag, r]));
      filled.forEach(input => {
        const itemResult = resultsMap.get(input.dataset.tag);
        const row = input.closest("tr");
        const statusBadge = row?.querySelector(".status-badge");
        if (itemResult) {
          if (itemResult.status === "applied") {
            input.value = "";
            input.classList.remove("valid", "invalid");
            if (statusBadge) {
              statusBadge.className = "badge status-badge applied";
              statusBadge.textContent = "Gravado com sucesso";
            }
          } else {
            input.classList.remove("valid");
            input.classList.add("invalid");
            if (statusBadge) {
              statusBadge.className = "badge status-badge rejected";
              statusBadge.textContent = itemResult.error || `Rejeitado: ${itemResult.status}`;
            }
          }
        }
      });

      updateWriteReviewButton();
      message(`Operação de escrita concluída: ${(res.results || []).length} tag(s) processada(s).`, "success");
      await loadWriteAuditEvents(eqId);
      await refreshWriteReadingsOnly(eqId);
    } catch (err) {
      confirmBtn.disabled = false;
      confirmBtn.textContent = "Confirmar e Gravar";
      message(`Falha na gravação: ${err.message}`, "error");
    }
  }

  async function loadWriteAuditEvents(eqId) {
    const tbody = el("write-audit-body");
    if (!tbody) return;
    try {
      const data = await api(`/api/v1/write-operation/audit?equipment_id=${encodeURIComponent(eqId)}&limit=25`);
      renderWriteAuditTable(data.audit_events || []);
    } catch (err) {
      console.warn("Could not load write audit events:", err);
    }
  }

  function renderWriteAuditTable(events) {
    const tbody = el("write-audit-body");
    if (!tbody) return;
    tbody.replaceChildren();

    if (!events.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 7;
      td.className = "muted";
      td.textContent = "Nenhum evento de auditoria registrado para este equipamento.";
      tr.append(td);
      tbody.append(tr);
      return;
    }

    events.forEach(ev => {
      const det = ev.detail || {};
      const tr = document.createElement("tr");

      const tdTs = document.createElement("td");
      tdTs.textContent = formatOpcTimestamp(det.timestamp || ev.occurred_at);

      const tdTag = document.createElement("td");
      tdTag.textContent = det.tag || "—";
      tdTag.style.fontWeight = "600";

      const tdPrev = document.createElement("td");
      tdPrev.textContent = det.previous_value !== null && det.previous_value !== undefined ? String(det.previous_value) : "—";

      const tdReq = document.createElement("td");
      tdReq.textContent = det.requested_value !== null && det.requested_value !== undefined ? String(det.requested_value) : "—";
      tdReq.style.fontWeight = "600";

      const tdUser = document.createElement("td");
      tdUser.textContent = det.user || "—";

      const tdStatus = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = "badge " + (det.status === "applied" ? "good" : "error");
      badge.textContent = det.status === "applied" ? "Gravado" : (det.status || "Erro");
      tdStatus.append(badge);

      const tdDetail = document.createElement("td");
      tdDetail.textContent = det.error ? det.error : "Sucesso";
      if (det.error) tdDetail.style.color = "var(--status-red)";

      tr.append(tdTs, tdTag, tdPrev, tdReq, tdUser, tdStatus, tdDetail);
      tbody.append(tr);
    });
  }

  async function refreshWriteReadingsOnly(eqId) {
    try {
      const data = await api(`/api/v1/write-operation/tags?equipment_id=${encodeURIComponent(eqId)}`);
      const newTags = data.tags || [];
      const tbody = el("write-spreadsheet-body");
      if (!tbody) return;

      newTags.forEach(ntag => {
        const existing = currentWriteTags.find(t => t.opc_item_path === ntag.opc_item_path);
        if (existing) {
          existing.current_value = ntag.current_value;
          existing.quality = ntag.quality;
          existing.timestamp = ntag.timestamp;
        }

        const row = tbody.querySelector(`tr[data-tag="${CSS.escape(ntag.opc_item_path)}"]`);
        if (row) {
          const tdCur = row.querySelector(".col-val");
          if (tdCur) {
            tdCur.replaceChildren();
            tdCur.textContent = ntag.current_value !== null && ntag.current_value !== undefined ? String(ntag.current_value) : "—";
            if (ntag.quality !== null && ntag.quality !== undefined) {
              const qBadge = document.createElement("span");
              qBadge.style.marginLeft = "6px";
              qBadge.className = "badge " + (ntag.quality >= 192 ? "good" : "error");
              qBadge.textContent = ntag.quality >= 192 ? "Good" : `Bad (${ntag.quality})`;
              tdCur.append(qBadge);
            }
          }
          const tdTs = row.querySelector(".col-ts");
          if (tdTs) {
            tdTs.textContent = formatOpcTimestamp(ntag.timestamp);
          }
        }
      });
    } catch (e) {
      // silent background refresh error
    }
  }

  // ==========================================
  // INITIALIZATION & EVENT LISTENERS
  // ==========================================

  async function init() {
    try {
      await api("/health");
      const [agentsData, capsData] = await Promise.all([
        api("/api/v1/agents"),
        api("/api/v1/ui-capabilities")
      ]);
      availableAgents = agentsData.agents || [];
      dispatch = capsData.dispatch_available;

      // Event listeners - Navigation Tabs
      el("tab-equipments").addEventListener("click", () => switchTab("equipments"));
      el("tab-opc").addEventListener("click", () => switchTab("opc"));
      el("tab-write-opc").addEventListener("click", () => switchTab("write-opc"));

      // Operação OPC Module events
      el("btn-refresh-write-opc").addEventListener("click", () => loadWriteEquipment());
      el("write-equipment-select").addEventListener("change", () => loadWriteEquipment());
      el("btn-refresh-write-reads").addEventListener("click", () => {
        const eqId = el("write-equipment-select").value;
        if (eqId) refreshWriteReadingsOnly(eqId);
      });
      el("btn-clear-write-values").addEventListener("click", clearWriteValues);
      el("btn-review-write").addEventListener("click", reviewWriteOperation);
      el("btn-refresh-audit").addEventListener("click", () => {
        const eqId = el("write-equipment-select").value;
        if (eqId) loadWriteAuditEvents(eqId);
      });
      el("btn-cancel-write-modal").addEventListener("click", () => {
        el("modal-write-confirm").hidden = true;
      });
      el("btn-confirm-execute-write").addEventListener("click", confirmAndExecuteWrite);

      // Equipments Module events
      el("btn-refresh-equipments").addEventListener("click", () => loadEquipments());
      el("btn-show-add-equipment").addEventListener("click", showAddEquipmentForm);
      el("btn-cancel-equipment").addEventListener("click", hideEquipmentForm);
      el("equipment-form").addEventListener("submit", e => {
        e.preventDefault();
        saveEquipment();
      });

      // OPC Module events
      el("btn-refresh-opc").addEventListener("click", () => onEquipmentSelected());
      el("opc-equipment-select").addEventListener("change", onEquipmentSelected);
      el("find-servers").addEventListener("click", findOpcServers);
      el("servers").addEventListener("change", () => {
        const val = el("servers").value;
        if (val) el("prog-id").value = val;
        stopLive();
        invalidateApproval();
      });
      el("prog-id").addEventListener("input", () => {
        stopLive();
        invalidateApproval();
      });
      el("interval").addEventListener("input", () => {
        stopLive();
        invalidateApproval();
      });
      el("add-tag").addEventListener("click", () => addTag());
      el("validate-all").addEventListener("click", () => validateTagList());
      el("btn-save-config").addEventListener("click", saveConfig);
      el("toggle-live").addEventListener("click", toggleLive);
      el("apply").addEventListener("click", applyToAgent);
      el("btn-load-active").addEventListener("click", () => {
        if (!activeAgentConfig) return;
        stopLive();
        invalidateApproval();
        el("config-name").value = `Ativa - v${activeAgentConfig.version}`;
        el("prog-id").value = activeAgentConfig.opc_prog_id;
        el("interval").value = activeAgentConfig.update_rate_ms;
        el("tags").replaceChildren();
        activeAgentConfig.tags.forEach(t => addTag(t));
        updateTagCountBadge();
        updateButtons();
      });

      // Window / Page lifecycle events: Auto-stop live monitoring
      document.addEventListener("visibilitychange", () => {
        if (document.hidden) stopLive();
      });
      window.addEventListener("pagehide", stopLive);
      window.addEventListener("beforeunload", stopLive);

      // Load initial equipment list
      await loadEquipments();
      addTag();
      switchTab("equipments");

      // Auto-refresh operational states every 5s
      autoRefreshTimer = setInterval(() => {
        if (currentTab === "equipments") {
          loadEquipments().catch(() => {});
        } else if (currentTab === "opc" && selectedEquipment) {
          if (selectedEquipment.agent_id) {
            loadAgentActiveConfigAndHistory(selectedEquipment.agent_id).catch(() => {});
          }
        } else if (currentTab === "write-opc") {
          const eqId = el("write-equipment-select").value;
          if (eqId) {
            refreshWriteReadingsOnly(eqId).catch(() => {});
          }
        }
      }, 5000);
    } catch (err) {
      message(err.message, "error");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
