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
  let selectedPiEquipment = null;
  let selectedPiConfig = null;
  let piMappings = [];
  let editingMappingId = null;
  let deletingMappingId = null;
  let publishingOnceMapping = null;

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
    timeout: "A inspeção excedeu o prazo.",
    interval_faster_than_opc: "A velocidade de publicação não pode ser menor que o intervalo de coleta OPC da configuração de origem.",
    mapping_disabled: "O mapeamento está desativado e não pode ser publicado no PI.",
    mapping_conflict: "Já existe um mapeamento cadastrado para esta tag nesta configuração.",
    tag_not_in_config: "A tag informada não pertence à configuração OPC selecionada.",
    invalid_pi_point_name: "Informe um nome válido para o PI Point (1 a 255 caracteres).",
    invalid_publish_interval_bounds: "A velocidade de publicação deve ser um inteiro entre 1000 e 60000 ms."
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
    const tabs = ["equipments", "opc", "pi"];
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
    } else if (tab === "pi") {
      loadPiIntegrationStatus().catch(() => {});
      populatePiEquipmentDropdown();
      if (selectedEquipment && selectedEquipment.equipment_id) {
        el("pi-equipment-select").value = selectedEquipment.equipment_id;
      }
      if (el("pi-equipment-select").value) {
        onPiEquipmentSelected().catch(e => message(e.message, "error"));
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
      populatePiEquipmentDropdown();
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
  // MODULE 3: INTEGRAÇÃO PI
  // ==========================================

  function populatePiEquipmentDropdown() {
    const sel = el("pi-equipment-select");
    if (!sel) return;
    const currentVal = sel.value;
    sel.replaceChildren();

    const optDefault = document.createElement("option");
    optDefault.value = "";
    optDefault.textContent = "Selecione um equipamento...";
    sel.appendChild(optDefault);

    equipments.forEach(eq => {
      const opt = document.createElement("option");
      opt.value = eq.equipment_id;
      opt.textContent = `${eq.name} (${eq.ip_address || "sem IP"})`;
      sel.appendChild(opt);
    });

    if (currentVal && equipments.some(e => e.equipment_id === currentVal)) {
      sel.value = currentVal;
    }
  }

  async function onPiEquipmentSelected() {
    const eqSelect = el("pi-equipment-select");
    const cfgSelect = el("pi-config-select");
    const tagSelect = el("pi-tag-select");
    const eqId = eqSelect ? eqSelect.value : "";

    selectedPiEquipment = equipments.find(e => e.equipment_id === eqId) || null;
    selectedPiConfig = null;
    editingMappingId = null;
    piMappings = [];

    // Reset config dropdown
    if (cfgSelect) {
      cfgSelect.replaceChildren();
      const optDef = document.createElement("option");
      optDef.value = "";
      optDef.textContent = "Selecione a configuração OPC...";
      cfgSelect.appendChild(optDef);
      cfgSelect.disabled = true;
    }

    // Reset tag dropdown & dependent form fields
    if (tagSelect) {
      tagSelect.replaceChildren();
      const optDef = document.createElement("option");
      optDef.value = "";
      optDef.textContent = "Selecione a tag OPC...";
      tagSelect.appendChild(optDef);
      tagSelect.disabled = true;
    }

    cancelEditMapping();
    renderPiMappingsTable();

    if (!selectedPiEquipment) return;

    try {
      const data = await api(`/api/v1/opc-configs?equipment_id=${encodeURIComponent(eqId)}`);
      const configs = data.configs || [];

      if (cfgSelect) {
        configs.forEach(cfg => {
          const opt = document.createElement("option");
          opt.value = cfg.config_id;
          opt.textContent = `${cfg.name} (${cfg.opc_prog_id}) [${cfg.update_rate_ms || cfg.interval_ms} ms]`;
          cfgSelect.appendChild(opt);
        });
        cfgSelect.disabled = configs.length === 0;
      }

      await loadPiMappings();
      await loadPiAudit();
    } catch (err) {
      message(err.message, "error");
    }
  }

  async function onPiConfigSelected() {
    const cfgSelect = el("pi-config-select");
    const cfgId = cfgSelect ? cfgSelect.value : "";
    const tagSelect = el("pi-tag-select");

    // Limpar seleções dependentes ao trocar configuração
    if (tagSelect) {
      tagSelect.replaceChildren();
      const optDef = document.createElement("option");
      optDef.value = "";
      optDef.textContent = "Selecione a tag OPC...";
      tagSelect.appendChild(optDef);
      tagSelect.disabled = true;
    }

    if (!editingMappingId && el("pi-point-name")) {
      el("pi-point-name").value = "";
    }

    if (!cfgId) {
      selectedPiConfig = null;
      validatePiForm(false);
      await loadPiMappings();
      return;
    }

    try {
      const res = await api(`/api/v1/opc-configs/${encodeURIComponent(cfgId)}`);
      const cfg = res.config || res;
      selectedPiConfig = cfg;

      // Popular tags da configuração selecionada
      if (tagSelect) {
        tagSelect.replaceChildren();
        const optDef = document.createElement("option");
        optDef.value = "";
        optDef.textContent = "Selecione a tag OPC...";
        tagSelect.appendChild(optDef);

        const tags = cfg.tags || [];
        tags.forEach(t => {
          const opt = document.createElement("option");
          opt.value = t;
          opt.textContent = t;
          tagSelect.appendChild(opt);
        });
        tagSelect.disabled = tags.length === 0;
      }

      // Sincronizar velocidade padrão com o intervalo OPC
      if (!editingMappingId) {
        const opcInterval = cfg.update_rate_ms || cfg.interval_ms || 5000;
        el("pi-interval").value = Math.max(opcInterval, 1000);
      }

      validatePiForm(false);
      await loadPiMappings();
    } catch (err) {
      message(err.message, "error");
    }
  }

  function validatePiForm(showMissingFields = false) {
    const errorBox = el("pi-form-error");
    const intervalInput = el("pi-interval");
    const eqVal = el("pi-equipment-select") ? el("pi-equipment-select").value : "";
    const cfgVal = el("pi-config-select") ? el("pi-config-select").value : "";
    const tagVal = el("pi-tag-select") ? el("pi-tag-select").value : "";
    const pointVal = el("pi-point-name") ? el("pi-point-name").value.trim() : "";
    const sourceVal = el("pi-point-source") ? el("pi-point-source").value.trim() : "";
    const loc1Val = el("pi-location1") ? el("pi-location1").value.trim() : "";
    const intervalVal = intervalInput ? parseInt(intervalInput.value, 10) : NaN;

    if (!errorBox) return false;

    // 1. Validar intervalo entre 1000 e 60000 ms
    if (isNaN(intervalVal) || intervalVal < 1000 || intervalVal > 60000) {
      errorBox.textContent = "A velocidade de publicação deve ser um número inteiro entre 1000 e 60000 ms.";
      errorBox.hidden = false;
      return false;
    }

    // 2. Validar velocidade maior ou igual ao update_rate_ms da configuração OPC escolhida
    const opcRate = selectedPiConfig ? (selectedPiConfig.update_rate_ms || selectedPiConfig.interval_ms || 1000) : 1000;
    if (selectedPiConfig && intervalVal < opcRate) {
      errorBox.textContent = `A velocidade de publicação (${intervalVal} ms) não pode ser menor que o intervalo de coleta OPC (${opcRate} ms) da configuração de origem.`;
      errorBox.hidden = false;
      return false;
    }

    // 3. Validar preenchimento dos campos obrigatórios
    if (showMissingFields) {
      if (!eqVal || !cfgVal || !tagVal || !pointVal || !sourceVal || loc1Val === "") {
        errorBox.textContent = "Preencha todos os campos obrigatórios: Equipamento, Configuração, Tag OPC, PI Point, Point Source e Location1.";
        errorBox.hidden = false;
        return false;
      }
    }

    errorBox.textContent = "";
    errorBox.hidden = true;
    return true;
  }

  async function loadPiMappings() {
    if (!selectedPiEquipment) {
      piMappings = [];
      renderPiMappingsTable();
      return;
    }
    try {
      let url = `/api/v1/pi-mappings?equipment_id=${encodeURIComponent(selectedPiEquipment.equipment_id)}`;
      if (selectedPiConfig && selectedPiConfig.config_id) {
        url += `&opc_config_id=${encodeURIComponent(selectedPiConfig.config_id)}`;
      }
      const data = await api(url);
      piMappings = data.mappings || [];
      renderPiMappingsTable();
    } catch (err) {
      message(err.message, "error");
    }
  }

  function renderPiMappingsTable() {
    const tbody = el("pi-mappings-body");
    const badge = el("pi-mapping-count-badge");
    if (!tbody) return;

    if (badge) {
      badge.textContent = `${piMappings.length} mapeamento(s)`;
    }

    tbody.replaceChildren();

    if (!selectedPiEquipment) {
      const tr = document.createElement("tr");
      tr.innerHTML = '<td colspan="14" class="muted">Selecione um equipamento para visualizar os mapeamentos.</td>';
      tbody.appendChild(tr);
      return;
    }

    if (piMappings.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = '<td colspan="14" class="muted">Nenhum mapeamento PI cadastrado para este equipamento. Adicione um acima.</td>';
      tbody.appendChild(tr);
      return;
    }

    piMappings.forEach(m => {
      const tr = document.createElement("tr");
      tr.id = `pi-mapping-row-${m.mapping_id}`;

      // 1. Tag OPC origem
      const tdTag = document.createElement("td");
      const codeTag = document.createElement("code");
      codeTag.textContent = m.opc_item_path;
      tdTag.appendChild(codeTag);
      tr.appendChild(tdTag);

      // 2. Último valor OPC
      const tdVal = document.createElement("td");
      tdVal.className = "col-val";
      tdVal.textContent = m.current_value !== null && m.current_value !== undefined ? String(m.current_value) : "—";
      tr.appendChild(tdVal);

      // 3. Qualidade
      const tdQual = document.createElement("td");
      tdQual.className = "col-qual";
      if (m.quality === null || m.quality === undefined) {
        tdQual.textContent = "—";
      } else if (m.quality >= 192) {
        tdQual.innerHTML = '<span class="badge good">Good</span>';
      } else {
        tdQual.innerHTML = `<span class="badge error">Bad (${m.quality})</span>`;
      }
      tr.appendChild(tdQual);

      // 4. Último timestamp OPC
      const tdTs = document.createElement("td");
      tdTs.className = "col-ts";
      tdTs.textContent = formatOpcTimestamp(m.opc_timestamp);
      tr.appendChild(tdTs);

      // 5. PI Point destino
      const tdPoint = document.createElement("td");
      const strongPoint = document.createElement("strong");
      strongPoint.textContent = m.pi_point_name;
      tdPoint.appendChild(strongPoint);
      tr.appendChild(tdPoint);

      // 6. Point Source
      const tdPs = document.createElement("td");
      tdPs.textContent = m.point_source || "OPC";
      tr.appendChild(tdPs);

      // 7. Location1
      const tdLoc = document.createElement("td");
      tdLoc.textContent = m.location1 !== null && m.location1 !== undefined ? String(m.location1) : "—";
      tr.appendChild(tdLoc);

      // 8. Velocidade
      const tdSpeed = document.createElement("td");
      tdSpeed.textContent = `${m.publish_interval_ms} ms`;
      tr.appendChild(tdSpeed);

      // 9. Estado
      const tdState = document.createElement("td");
      tdState.innerHTML = m.enabled
        ? '<span class="badge active-status">Ativo</span>'
        : '<span class="badge inactive-status">Inativo</span>';
      tr.appendChild(tdState);

      // 10. Último resultado
      const tdRes = document.createElement("td");
      const st = String(m.last_publish_status || "").toLowerCase();
      if (st === "simulado" || st === "simulated") {
        const tsFormatted = formatOpcTimestamp(m.last_published_at);
        tdRes.innerHTML = `<span class="badge simulated" title="Valor: ${m.last_published_value || ''}">Simulado (${tsFormatted})</span>`;
      } else if (st === "publicado" || st === "published") {
        const tsFormatted = formatOpcTimestamp(m.last_published_at);
        tdRes.innerHTML = `<span class="badge good" title="Valor: ${m.last_published_value || ''}">Publicado (${tsFormatted})</span>`;
      } else if (st === "erro" || st === "error") {
        tdRes.innerHTML = `<span class="badge error" title="${m.last_publish_error || ''}">Erro</span>`;
      } else if (st === "desabilitado" || st === "disabled") {
        tdRes.innerHTML = '<span class="badge muted">Desabilitado</span>';
      } else {
        tdRes.innerHTML = '<span class="badge unconfigured">Não configurado</span>';
      }
      tr.appendChild(tdRes);

      // 11. Último envio
      const tdLastPub = document.createElement("td");
      tdLastPub.className = "col-ts";
      tdLastPub.textContent = m.last_published_at ? formatOpcTimestamp(m.last_published_at) : "—";
      tr.appendChild(tdLastPub);

      // 12. Próximo envio
      const tdNextPub = document.createElement("td");
      tdNextPub.className = "col-ts";
      tdNextPub.textContent = m.next_publish_due_at ? formatOpcTimestamp(m.next_publish_due_at) : "—";
      tr.appendChild(tdNextPub);

      // 13. Erro
      const tdErr = document.createElement("td");
      if (m.last_publish_error) {
        const spanErr = document.createElement("span");
        spanErr.className = "badge error";
        spanErr.title = m.last_publish_error;
        spanErr.textContent = m.last_publish_error.length > 25 ? m.last_publish_error.slice(0, 22) + "..." : m.last_publish_error;
        tdErr.appendChild(spanErr);
      } else {
        tdErr.textContent = "—";
      }
      tr.appendChild(tdErr);

      // 14. Ações
      const tdAct = document.createElement("td");
      tdAct.className = "actions";

      // Botão Publicar uma vez
      const btnPubOnce = document.createElement("button");
      btnPubOnce.type = "button";
      btnPubOnce.className = "btn-sm primary";
      btnPubOnce.textContent = "Publicar uma vez";
      btnPubOnce.title = "Publicar a leitura atual em cache no PI Point de destino";
      btnPubOnce.disabled = !m.enabled;
      btnPubOnce.addEventListener("click", () => openPublishOnceModal(m));
      tdAct.appendChild(btnPubOnce);

      // Botão Simular
      const btnSim = document.createElement("button");
      btnSim.type = "button";
      btnSim.className = "btn-sm";
      btnSim.textContent = "Simular";
      btnSim.title = "Simular publicação no PI usando exclusivamente o valor em cache";
      btnSim.disabled = !m.enabled;
      btnSim.addEventListener("click", () => simulatePiMapping(m.mapping_id));
      tdAct.appendChild(btnSim);

      // Botão Ativar / Desativar
      const btnToggle = document.createElement("button");
      btnToggle.type = "button";
      btnToggle.className = "btn-sm";
      btnToggle.textContent = m.enabled ? "Desativar" : "Ativar";
      btnToggle.addEventListener("click", () => togglePiMapping(m.mapping_id, m.enabled));
      tdAct.appendChild(btnToggle);

      // Botão Editar
      const btnEdit = document.createElement("button");
      btnEdit.type = "button";
      btnEdit.className = "btn-sm";
      btnEdit.textContent = "Editar";
      btnEdit.addEventListener("click", () => editPiMapping(m));
      tdAct.appendChild(btnEdit);

      // Botão Excluir
      const btnDel = document.createElement("button");
      btnDel.type = "button";
      btnDel.className = "btn-sm danger";
      btnDel.textContent = "Excluir";
      btnDel.addEventListener("click", () => confirmDeleteMapping(m));
      tdAct.appendChild(btnDel);

      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    });
  }

  async function savePiMapping() {
    if (!validatePiForm(true)) return;

    const eqSelect = el("pi-equipment-select");
    const cfgSelect = el("pi-config-select");
    const tagSelect = el("pi-tag-select");
    const pointInput = el("pi-point-name");
    const sourceInput = el("pi-point-source");
    const locInput = el("pi-location1");
    const intervalInput = el("pi-interval");
    const enabledInput = el("pi-mapping-enabled");
    const formId = el("mapping-form-id").value;

    const eqId = eqSelect ? eqSelect.value : "";
    const cfgId = cfgSelect ? cfgSelect.value : "";
    const opcTag = tagSelect ? tagSelect.value : "";
    const piPointName = pointInput ? pointInput.value.trim() : "";
    const pointSource = sourceInput ? (sourceInput.value.trim() || "OPC") : "OPC";
    const location1 = locInput ? parseInt(locInput.value, 10) : 1;
    const publishIntervalMs = intervalInput ? parseInt(intervalInput.value, 10) : 5000;
    const enabled = enabledInput ? enabledInput.checked : true;

    const payload = {
      equipment_id: eqId,
      opc_config_id: cfgId,
      opc_item_path: opcTag,
      pi_point_name: piPointName,
      point_source: pointSource,
      location1: isNaN(location1) ? 1 : location1,
      publish_interval_ms: publishIntervalMs,
      enabled: enabled
    };

    try {
      if (formId) {
        await api(`/api/v1/pi-mappings/${encodeURIComponent(formId)}`, payload, "PUT");
        message("Mapeamento PI atualizado com sucesso.", "success");
      } else {
        await api("/api/v1/pi-mappings", payload, "POST");
        message("Mapeamento PI salvo com sucesso.", "success");
      }
      cancelEditMapping();
      await loadPiMappings();
      await loadPiAudit();
    } catch (err) {
      message(err.message, "error");
    }
  }

  async function editPiMapping(m) {
    editingMappingId = m.mapping_id;
    el("mapping-form-id").value = m.mapping_id;
    el("mapping-card-title").textContent = "Mapeamentos OPC → PI";
    el("mapping-form-badge").textContent = "Editar mapeamento";
    el("btn-save-mapping").textContent = "Salvar alterações";
    el("btn-cancel-mapping").hidden = false;

    // Equipamento em cascata
    el("pi-equipment-select").value = m.equipment_id;
    selectedPiEquipment = equipments.find(e => e.equipment_id === m.equipment_id) || null;

    try {
      // Carregar configurações do equipamento
      const data = await api(`/api/v1/opc-configs?equipment_id=${encodeURIComponent(m.equipment_id)}`);
      const configs = data.configs || [];
      const cfgSelect = el("pi-config-select");
      cfgSelect.replaceChildren();
      const optDef = document.createElement("option");
      optDef.value = "";
      optDef.textContent = "Selecione a configuração OPC...";
      cfgSelect.appendChild(optDef);
      configs.forEach(cfg => {
        const opt = document.createElement("option");
        opt.value = cfg.config_id;
        opt.textContent = `${cfg.name} (${cfg.opc_prog_id}) [${cfg.update_rate_ms || cfg.interval_ms} ms]`;
        cfgSelect.appendChild(opt);
      });
      cfgSelect.disabled = false;
      cfgSelect.value = m.opc_config_id;

      // Carregar tags da configuração
      const res = await api(`/api/v1/opc-configs/${encodeURIComponent(m.opc_config_id)}`);
      const cfg = res.config || res;
      selectedPiConfig = cfg;
      const tagSelect = el("pi-tag-select");
      tagSelect.replaceChildren();
      const optDefTag = document.createElement("option");
      optDefTag.value = "";
      optDefTag.textContent = "Selecione a tag OPC...";
      tagSelect.appendChild(optDefTag);
      const tags = cfg.tags || [];
      tags.forEach(t => {
        const opt = document.createElement("option");
        opt.value = t;
        opt.textContent = t;
        tagSelect.appendChild(opt);
      });
      tagSelect.disabled = false;
      tagSelect.value = m.opc_item_path;
    } catch (err) {
      message(err.message, "error");
    }

    if (el("pi-point-name")) el("pi-point-name").value = m.pi_point_name;
    if (el("pi-point-source")) el("pi-point-source").value = m.point_source || "OPC";
    if (el("pi-location1")) el("pi-location1").value = m.location1 !== null && m.location1 !== undefined ? m.location1 : 1;
    if (el("pi-interval")) el("pi-interval").value = m.publish_interval_ms;
    if (el("pi-mapping-enabled")) el("pi-mapping-enabled").checked = !!m.enabled;

    validatePiForm(false);
    el("pi-mapping-form").scrollIntoView({ behavior: "smooth" });
  }

  function cancelEditMapping() {
    editingMappingId = null;
    el("mapping-form-id").value = "";
    el("mapping-card-title").textContent = "Mapeamentos OPC → PI";
    el("mapping-form-badge").textContent = "Criar mapeamento";
    el("btn-save-mapping").textContent = "Salvar mapeamento";
    el("btn-cancel-mapping").hidden = true;
    if (el("pi-point-name")) el("pi-point-name").value = "";
    if (el("pi-point-source")) el("pi-point-source").value = "OPC";
    if (el("pi-location1")) el("pi-location1").value = "1";
    if (selectedPiConfig && el("pi-interval")) {
      el("pi-interval").value = Math.max(selectedPiConfig.update_rate_ms || selectedPiConfig.interval_ms || 1000, 1000);
    } else if (el("pi-interval")) {
      el("pi-interval").value = 5000;
    }
    if (el("pi-mapping-enabled")) el("pi-mapping-enabled").checked = true;
    const errorBox = el("pi-form-error");
    if (errorBox) {
      errorBox.textContent = "";
      errorBox.hidden = true;
    }
  }

  async function togglePiMapping(mappingId, currentEnabled) {
    try {
      await api(`/api/v1/pi-mappings/${encodeURIComponent(mappingId)}/toggle`, { enabled: !currentEnabled }, "POST");
      message(`Mapeamento ${!currentEnabled ? "ativado" : "desativado"} com sucesso.`, "success");
      await loadPiMappings();
      await loadPiAudit();
    } catch (err) {
      message(err.message, "error");
    }
  }

  function confirmDeleteMapping(m) {
    deletingMappingId = m.mapping_id;
    el("delete-mapping-point-name").textContent = m.pi_point_name;
    el("modal-delete-mapping-confirm").hidden = false;
  }

  async function executeDeleteMapping() {
    if (!deletingMappingId) return;
    try {
      await api(`/api/v1/pi-mappings/${encodeURIComponent(deletingMappingId)}`, undefined, "DELETE");
      message("Mapeamento excluído com sucesso.", "success");
      el("modal-delete-mapping-confirm").hidden = true;
      if (editingMappingId === deletingMappingId) {
        cancelEditMapping();
      }
      deletingMappingId = null;
      await loadPiMappings();
      await loadPiAudit();
    } catch (err) {
      message(err.message, "error");
    }
  }

  async function simulatePiMapping(mappingId) {
    try {
      const res = await api(`/api/v1/pi-mappings/${encodeURIComponent(mappingId)}/simulate`, {}, "POST");
      message(`Simulação PI realizada com sucesso: PI Point "${res.pi_point_name}" recebeu valor "${res.value_written}" (Status: ${res.publish_status}).`, "success");
      await loadPiMappings();
      await loadPiAudit();
    } catch (err) {
      message(err.message, "error");
    }
  }

  async function loadPiIntegrationStatus() {
    try {
      const data = await api("/api/v1/pi-integration/status");
      const banner = el("pi-simulation-banner");
      if (banner) {
        if (data.output_enabled) {
          banner.className = "banner success";
          banner.textContent = data.banner_text || "Saída PI habilitada";
        } else {
          banner.className = "banner info";
          banner.textContent = data.banner_text || "Saída PI: Simulação — nenhuma escrita real habilitada.";
        }
      }
    } catch (e) {
      // Ignore if unavailable
    }
  }

  async function testPiConnection() {
    const btn = el("btn-test-pi-connection");
    if (btn) btn.disabled = true;
    try {
      const res = await api("/api/v1/pi-integration/test-connection", {}, "POST");
      if (res.connected) {
        message(res.message || "Conexão PI verificada com sucesso.", "success");
      } else {
        message(res.message || "Falha na conexão com PI.", "warning");
      }
    } catch (err) {
      message(err.message || "Erro ao testar conexão PI.", "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function openPublishOnceModal(m) {
    publishingOnceMapping = m;
    const modal = el("modal-publish-once-confirm");
    if (!modal) return;

    if (el("publish-once-point")) el("publish-once-point").textContent = m.pi_point_name || "—";
    if (el("publish-once-tag")) el("publish-once-tag").textContent = m.opc_item_path || "—";
    if (el("publish-once-value")) el("publish-once-value").textContent = m.current_value !== null && m.current_value !== undefined ? String(m.current_value) : "—";
    if (el("publish-once-quality")) el("publish-once-quality").textContent = m.quality_text || (m.quality !== null && m.quality !== undefined ? String(m.quality) : "—");
    if (el("publish-once-timestamp")) el("publish-once-timestamp").textContent = formatOpcTimestamp(m.opc_timestamp);

    const warnBox = el("publish-once-warning");
    const confirmBtn = el("btn-confirm-publish-once");

    let blockReason = null;
    if (m.current_value === null || m.current_value === undefined) {
      blockReason = "Publicação bloqueada: nenhum valor OPC coletado em cache para esta tag.";
    } else if (m.quality !== null && m.quality !== undefined && m.quality < 192) {
      blockReason = `Publicação bloqueada: qualidade OPC não é confiável (Bad: ${m.quality}).`;
    } else if (m.stale) {
      blockReason = "Publicação bloqueada: leitura OPC está desatualizada (stale).";
    }

    if (warnBox) {
      if (blockReason) {
        warnBox.textContent = blockReason;
        warnBox.hidden = false;
      } else {
        warnBox.hidden = true;
      }
    }
    if (confirmBtn) {
      confirmBtn.disabled = Boolean(blockReason);
    }

    modal.hidden = false;
  }

  function closePublishOnceModal() {
    publishingOnceMapping = null;
    const modal = el("modal-publish-once-confirm");
    if (modal) modal.hidden = true;
  }

  async function confirmPublishOnce() {
    if (!publishingOnceMapping) return;
    const btn = el("btn-confirm-publish-once");
    if (btn) btn.disabled = true;
    try {
      const res = await api(`/api/v1/pi-mappings/${encodeURIComponent(publishingOnceMapping.mapping_id)}/publish-once`, {}, "POST");
      closePublishOnceModal();
      const st = res.result ? res.result.status : "Publicado";
      const val = res.result ? res.result.value : publishingOnceMapping.current_value;
      if (st === "Erro") {
        message(`Publicação no PI retornou erro: ${res.result.error || 'Falha de comunicação'}`, "error");
      } else {
        message(`Publicação no PI concluída com sucesso: "${publishingOnceMapping.pi_point_name}" = ${val} (${st}).`, "success");
      }
      await loadPiMappings();
      await loadPiAudit();
    } catch (err) {
      message(err.message, "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function loadPiAudit() {
    const tbody = el("pi-audit-body");
    if (!tbody) return;
    try {
      const data = await api("/api/v1/pi-mappings/audit");
      const events = data.audit_events || data.events || [];
      tbody.replaceChildren();

      if (events.length === 0) {
        const tr = document.createElement("tr");
        tr.innerHTML = '<td colspan="7" class="muted">Nenhum evento registrado.</td>';
        tbody.appendChild(tr);
        return;
      }

      events.forEach(ev => {
        const tr = document.createElement("tr");

        // 1. Data/Hora
        const tdTs = document.createElement("td");
        tdTs.textContent = formatOpcTimestamp(ev.occurred_at || ev.timestamp);
        tr.appendChild(tdTs);

        // 2. Evento
        const tdEv = document.createElement("td");
        tdEv.textContent = ev.event_type;
        tr.appendChild(tdEv);

        // 3. PI Point
        const det = ev.detail || ev.details || {};
        const tdPoint = document.createElement("td");
        tdPoint.textContent = det.pi_point_name || ev.pi_point_name || "—";
        tr.appendChild(tdPoint);

        // 4. Tag OPC
        const tdTag = document.createElement("td");
        tdTag.textContent = det.opc_item_path || ev.opc_item_path || "—";
        tr.appendChild(tdTag);

        // 5. Valor simulado
        const tdVal = document.createElement("td");
        tdVal.textContent = det.value !== undefined && det.value !== null ? String(det.value) : "—";
        tr.appendChild(tdVal);

        // 6. Operador
        const tdUser = document.createElement("td");
        tdUser.textContent = det.user || ev.user || "admin";
        tr.appendChild(tdUser);

        // 7. Situação
        const tdSit = document.createElement("td");
        if (ev.event_type === "pi_mapping.simulation" || ev.event_type === "pi_simulation_success") {
          tdSit.innerHTML = '<span class="badge simulated">Simulado</span>';
        } else if (ev.event_type === "pi_mapping.created" || ev.event_type === "pi_mapping.enabled") {
          tdSit.innerHTML = '<span class="badge active-status">Ativo</span>';
        } else if (ev.event_type === "pi_mapping.disabled" || ev.event_type === "pi_mapping.deleted") {
          tdSit.innerHTML = '<span class="badge inactive-status">Inativo</span>';
        } else {
          tdSit.innerHTML = '<span class="badge info">OK</span>';
        }
        tr.appendChild(tdSit);

        tbody.appendChild(tr);
      });
    } catch (err) {
      // Background audit fetch non-fatal
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
      el("tab-pi").addEventListener("click", () => switchTab("pi"));

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

      // PI Integration Module events
      el("pi-equipment-select").addEventListener("change", onPiEquipmentSelected);
      el("pi-config-select").addEventListener("change", onPiConfigSelected);
      el("pi-tag-select").addEventListener("change", () => validatePiForm(false));
      el("pi-point-name").addEventListener("input", () => validatePiForm(false));
      el("pi-point-source").addEventListener("input", () => validatePiForm(false));
      el("pi-location1").addEventListener("input", () => validatePiForm(false));
      el("pi-interval").addEventListener("input", () => validatePiForm(false));
      el("pi-mapping-form").addEventListener("submit", e => {
        e.preventDefault();
        savePiMapping();
      });
      el("btn-cancel-mapping").addEventListener("click", cancelEditMapping);
      el("btn-refresh-pi-audit").addEventListener("click", loadPiAudit);
      el("btn-cancel-delete-modal").addEventListener("click", () => {
        deletingMappingId = null;
        el("modal-delete-mapping-confirm").hidden = true;
      });
      el("btn-confirm-delete-mapping").addEventListener("click", executeDeleteMapping);
      if (el("btn-test-pi-connection")) {
        el("btn-test-pi-connection").addEventListener("click", testPiConnection);
      }
      if (el("btn-cancel-publish-once-modal")) {
        el("btn-cancel-publish-once-modal").addEventListener("click", closePublishOnceModal);
      }
      if (el("btn-confirm-publish-once")) {
        el("btn-confirm-publish-once").addEventListener("click", confirmPublishOnce);
      }

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

      // Auto-refresh operational states: only while on "pi" tab; no auto-refresh on Equipamentos and OPC
      autoRefreshTimer = setInterval(() => {
        if (currentTab === "pi" && selectedPiEquipment) {
          loadPiMappings().catch(() => {});
          loadPiAudit().catch(() => {});
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
