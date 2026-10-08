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
  let selectedPiProgId = "";
  let currentPiProfile = null;
  let piMappings = [];
  let deletingMappingId = null;
  let publishingOnceMapping = null;
  let piOutputEnabled = false;
  let piActiveCell = null;
  let piSelectedCell = null;
  let piSelectionRange = null;
  let piIsEditing = false;
  let piIsMouseDown = false;
  let piMouseDownPos = null;
  let piIsDraggingSelection = false;
  let piDragAnchor = null;
  let piIsDraggingHandle = false;
  let piDragHandleSourceRange = null;
  let piDragTargetRow = null;
  let piHasPendingChanges = false;
  let piCellErrors = new Map();
  let piEditingCellVal = null;

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

  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

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
    try {
      stopLive();
    } catch (_) {}

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

    try {
      if (tab === "equipments") {
        loadEquipments().catch(e => message(`Erro ao carregar equipamentos: ${e.message}`, "error"));
      } else if (tab === "opc") {
        populateEquipmentDropdown();
        const sel = el("opc-equipment-select");
        if (sel && sel.value) {
          onEquipmentSelected().catch(e => message(`Erro ao carregar configuração OPC: ${e.message}`, "error"));
        }
      } else if (tab === "pi") {
        loadPiIntegrationStatus().catch(() => {});
        populatePiEquipmentDropdown();
        const piSel = el("pi-equipment-select");
        if (selectedEquipment && selectedEquipment.equipment_id && piSel) {
          piSel.value = selectedEquipment.equipment_id;
        }
        if (piSel && piSel.value) {
          onPiEquipmentSelected().catch(e => message(`Erro na Integração PI: ${e.message}`, "error"));
        }
      }
    } catch (errTab) {
      message(`Erro ao alternar para a aba ${tab}: ${errTab.message}`, "error");
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

  function clearProfileError() {
    const errBox = el("pi-profile-error");
    if (errBox) {
      errBox.textContent = "";
      errBox.hidden = true;
    }
  }

  function clearSheetMessages() {
    const errBox = el("pi-sheet-error");
    if (errBox) {
      errBox.textContent = "";
      errBox.hidden = true;
    }
    const succBox = el("pi-sheet-success");
    if (succBox) {
      succBox.textContent = "";
      succBox.hidden = true;
    }
  }

  function showSheetError(msg) {
    const errBox = el("pi-sheet-error");
    if (errBox) {
      errBox.innerHTML = msg;
      errBox.hidden = false;
    }
    const succBox = el("pi-sheet-success");
    if (succBox) succBox.hidden = true;
  }

  function showSheetSuccess(msg) {
    const succBox = el("pi-sheet-success");
    if (succBox) {
      succBox.innerHTML = msg;
      succBox.hidden = false;
      setTimeout(() => { if (succBox) succBox.hidden = true; }, 4000);
    }
    const errBox = el("pi-sheet-error");
    if (errBox) errBox.hidden = true;
  }

  function updateRowCountBadge() {
    const tbody = el("pi-spreadsheet-body");
    const countBadge = el("pi-mapping-count-badge");
    if (tbody && countBadge) {
      const rows = [...tbody.querySelectorAll("tr")].filter(r => !r.classList.contains("empty-row"));
      countBadge.textContent = `${rows.length} linha(s)`;
    }
  }

  function updateSpreadsheetToolbar(profileActive) {
    const btnAdd = el("btn-add-pi-row");
    const btnRead = el("btn-read-now-pi");
    if (btnAdd) btnAdd.disabled = !profileActive;
    if (btnRead) btnRead.disabled = !profileActive || !selectedPiEquipment?.agent_id;
    updateSaveButtonState();
    updateDeleteButtonState();
  }

  function renderPiProfileState() {
    clearProfileError();
    const psInput = el("pi-point-source");
    const locInput = el("pi-location1");
    const enInput = el("pi-profile-enabled");
    const profIdInput = el("pi-profile-id");
    const btnSaveProf = el("btn-save-pi-profile");
    const badge = el("pi-profile-badge");
    const notice = el("pi-profile-notice");
    const sheetWarning = el("pi-sheet-warning");
    const inheritedBadge = el("pi-profile-inherited-badge");

    const hasContext = Boolean(selectedPiEquipment && selectedPiProgId);

    if (!hasContext) {
      if (psInput) { psInput.value = "OPCBRIDGE"; psInput.disabled = true; }
      if (locInput) { locInput.value = "1"; locInput.disabled = true; }
      if (enInput) { enInput.checked = true; enInput.disabled = true; }
      if (profIdInput) profIdInput.value = "";
      if (btnSaveProf) btnSaveProf.disabled = true;
      if (badge) { badge.textContent = "Perfil PI"; badge.className = "badge"; }
      if (notice) { notice.textContent = ""; notice.hidden = true; }
      if (sheetWarning) {
        sheetWarning.textContent = "Selecione um equipamento e informe o Servidor OPC com perfil PI ativo para carregar e editar a planilha.";
        sheetWarning.hidden = false;
      }
      if (inheritedBadge) {
        inheritedBadge.textContent = "Point Source: — | Location1: —";
      }
      updateSpreadsheetToolbar(false);
      return;
    }

    // Context is selected: enable profile inputs
    if (psInput) psInput.disabled = false;
    if (locInput) locInput.disabled = false;
    if (enInput) enInput.disabled = false;
    if (btnSaveProf) btnSaveProf.disabled = false;

    if (currentPiProfile) {
      if (profIdInput) profIdInput.value = currentPiProfile.profile_id || currentPiProfile.id || "";
      if (psInput) psInput.value = currentPiProfile.point_source || "OPCBRIDGE";
      if (locInput) locInput.value = currentPiProfile.location1 !== null && currentPiProfile.location1 !== undefined ? currentPiProfile.location1 : 1;
      if (enInput) enInput.checked = Boolean(currentPiProfile.enabled);

      if (currentPiProfile.enabled) {
        if (badge) { badge.textContent = "Perfil Ativo"; badge.className = "badge active-status"; }
        if (notice) {
          notice.textContent = `Perfil ativo para este servidor OPC (Point Source: ${currentPiProfile.point_source}, Location1: ${currentPiProfile.location1}).`;
          notice.className = "banner success";
          notice.hidden = false;
        }
        if (sheetWarning) sheetWarning.hidden = true;
        if (inheritedBadge) {
          inheritedBadge.textContent = `Point Source: ${currentPiProfile.point_source} | Location1: ${currentPiProfile.location1}`;
        }
        updateSpreadsheetToolbar(true);
      } else {
        if (badge) { badge.textContent = "Perfil Inativo"; badge.className = "badge inactive-status"; }
        if (notice) {
          notice.textContent = "Perfil PI deste servidor OPC está desativado. Ative o perfil para permitir o envio dos mapeamentos.";
          notice.className = "banner warning";
          notice.hidden = false;
        }
        if (sheetWarning) {
          sheetWarning.textContent = "O perfil PI deste servidor OPC está desativado. Ative o perfil acima para habilitar a planilha.";
          sheetWarning.hidden = false;
        }
        if (inheritedBadge) {
          inheritedBadge.textContent = `Point Source: ${currentPiProfile.point_source} (Inativo) | Location1: ${currentPiProfile.location1}`;
        }
        updateSpreadsheetToolbar(false);
      }
    } else {
      if (profIdInput) profIdInput.value = "";
      if (psInput) psInput.value = "OPCBRIDGE";
      if (locInput) locInput.value = "1";
      if (enInput) enInput.checked = true;
      if (badge) { badge.textContent = "Não configurado"; badge.className = "badge unconfigured"; }
      if (notice) {
        notice.textContent = "Nenhum perfil PI cadastrado para este Servidor OPC. Defina Point Source e Location1 e salve o perfil.";
        notice.className = "banner warning";
        notice.hidden = false;
      }
      if (sheetWarning) {
        sheetWarning.textContent = "É obrigatório criar e ativar o perfil PI para este Equipamento e Servidor OPC antes de cadastrar mapeamentos na planilha.";
        sheetWarning.hidden = false;
      }
      if (inheritedBadge) {
        inheritedBadge.textContent = "Point Source: — | Location1: —";
      }
      updateSpreadsheetToolbar(false);
    }
  }

  async function savePiProfile() {
    clearProfileError();
    if (!selectedPiEquipment || !selectedPiProgId) {
      message("Selecione um equipamento e informe o Servidor OPC (ProgID) antes de salvar o perfil PI.", "error");
      return;
    }

    const psInput = el("pi-point-source");
    const locInput = el("pi-location1");
    const enInput = el("pi-profile-enabled");
    const errBox = el("pi-profile-error");

    const pointSource = psInput ? psInput.value.trim() : "";
    const locVal = locInput ? locInput.value.trim() : "";
    const location1 = parseInt(locVal, 10);
    const enabled = enInput ? enInput.checked : true;

    if (!pointSource || isNaN(location1)) {
      if (errBox) {
        errBox.textContent = "Point Source e Location1 (número inteiro) são obrigatórios.";
        errBox.hidden = false;
      }
      return;
    }

    if (currentPiProfile) {
      const oldPs = currentPiProfile.point_source || "";
      const oldLoc = currentPiProfile.location1;
      if (oldPs !== pointSource || oldLoc !== location1) {
        const proceed = confirm("Alterar o perfil PI irá desativar todos os mapeamentos deste servidor até que sejam novamente validados. Deseja continuar?");
        if (!proceed) return;
      }
    }

    const payload = {
      equipment_id: selectedPiEquipment.equipment_id,
      opc_prog_id: selectedPiProgId,
      point_source: pointSource,
      location1: location1,
      enabled: enabled
    };

    try {
      const res = await api("/api/v1/pi-profiles", payload, "POST");
      currentPiProfile = res.profile || null;
      message("Perfil PI salvo com sucesso.", "success");
      renderPiProfileState();
      await loadPiMappings();
      await loadPiAudit();
    } catch (err) {
      if (errBox) {
        errBox.textContent = err.message;
        errBox.hidden = false;
      }
      message(err.message, "error");
    }
  }

  async function onPiEquipmentSelected() {
    clearProfileError();
    clearSheetMessages();
    const eqSelect = el("pi-equipment-select");
    const progInput = el("pi-prog-id");
    const btnDiscover = el("btn-discover-pi-servers");
    const eqId = eqSelect ? eqSelect.value : "";

    selectedPiEquipment = equipments.find(e => e.equipment_id === eqId) || null;
    currentPiProfile = null;
    piMappings = [];

    // Clear datalist
    const datalist = el("pi-servers-list");
    if (datalist) datalist.replaceChildren();

    if (!selectedPiEquipment) {
      if (progInput) {
        progInput.value = "";
        progInput.disabled = true;
      }
      if (btnDiscover) btnDiscover.disabled = true;
      selectedPiProgId = "";
      renderPiProfileState();
      renderPiSpreadsheet();
      return;
    }

    if (progInput) {
      progInput.disabled = false;
    }
    if (btnDiscover) {
      btnDiscover.disabled = !selectedPiEquipment.agent_id || selectedPiEquipment.agent_status !== "connected";
    }

    selectedPiProgId = progInput ? progInput.value.trim() : "";
    if (selectedPiProgId) {
      await loadPiProfileAndSheet();
    } else {
      renderPiProfileState();
      renderPiSpreadsheet();
    }
    await loadPiAudit();
  }

  async function discoverPiServers() {
    if (!selectedPiEquipment?.agent_id || busy) return;
    const btn = el("btn-discover-pi-servers");
    const datalist = el("pi-servers-list");
    const progInput = el("pi-prog-id");
    try {
      if (btn) btn.disabled = true;
      message("Buscando servidores OPC disponíveis no equipamento...", "info");
      const data = await api(`/api/v1/pi-integration/discover-servers?equipment_id=${encodeURIComponent(selectedPiEquipment.equipment_id)}`);
      const servers = data.servers || [];
      if (datalist) {
        datalist.replaceChildren();
        servers.forEach(s => {
          const opt = document.createElement("option");
          opt.value = s;
          datalist.appendChild(opt);
        });
      }
      if (servers.length > 0) {
        message(`${servers.length} servidor(es) OPC descoberto(s). Selecione ou informe o ProgID.`, "success");
        if (progInput && !progInput.value.trim()) {
          progInput.value = servers[0];
          selectedPiProgId = servers[0];
          await loadPiProfileAndSheet();
        }
      } else {
        message("Nenhum servidor OPC anunciado pelo agente. Informe o ProgID manualmente.", "info");
      }
    } catch (err) {
      message(err.message, "error");
    } finally {
      if (btn) btn.disabled = !selectedPiEquipment?.agent_id;
    }
  }

  async function onPiProgIdChanged() {
    const progInput = el("pi-prog-id");
    const newProgId = progInput ? progInput.value.trim() : "";
    if (newProgId === selectedPiProgId) return;
    selectedPiProgId = newProgId;
    if (selectedPiEquipment && selectedPiProgId) {
      await loadPiProfileAndSheet();
    } else {
      currentPiProfile = null;
      piMappings = [];
      renderPiProfileState();
      renderPiSpreadsheet();
    }
  }

  async function loadPiProfileAndSheet() {
    if (!selectedPiEquipment || !selectedPiProgId) return;
    clearProfileError();
    clearSheetMessages();
    try {
      const profData = await api(`/api/v1/pi-profiles?equipment_id=${encodeURIComponent(selectedPiEquipment.equipment_id)}&opc_prog_id=${encodeURIComponent(selectedPiProgId)}`);
      currentPiProfile = profData.profile || null;
      renderPiProfileState();

      await loadPiMappings();
    } catch (err) {
      message(err.message, "error");
    }
  }

  async function validatePiPointMapping(mappingId) {
    try {
      message("Validando PI Point na PI Web API...", "info");
      const res = await api(`/api/v1/pi-mappings/${encodeURIComponent(mappingId)}/validate-point`, {}, "POST");
      if (res.valid) {
        message(`Ponto PI "${res.pi_point_name}" validado com sucesso! (PointSource: ${res.actual_point_source}, Location1: ${res.actual_location1})`, "success");
      } else {
        message(`Falha na validação do PI Point "${res.pi_point_name}": ${res.message || res.error}`, "warning");
      }
      await loadPiMappings();
      await loadPiAudit();
    } catch (err) {
      message(`Erro ao validar PI Point: ${err.message}`, "error");
    }
  }

  async function loadPiMappings() {
    if (!selectedPiEquipment || !selectedPiProgId) {
      piMappings = [];
      setPiPendingChanges(false);
      renderPiSpreadsheet();
      return;
    }
    try {
      const url = `/api/v1/pi-mappings?equipment_id=${encodeURIComponent(selectedPiEquipment.equipment_id)}&opc_prog_id=${encodeURIComponent(selectedPiProgId)}`;
      const data = await api(url);
      piMappings = data.mappings || [];
      setPiPendingChanges(false);
      renderPiSpreadsheet();
    } catch (err) {
      message(err.message, "error");
    }
  }

  // ==========================================
  // GOOGLE SHEETS / EXCEL STYLE SPREADSHEET ENGINE
  // ==========================================

  function getTableRows() {
    const tbody = el("pi-spreadsheet-body");
    if (!tbody) return [];
    return [...tbody.querySelectorAll("tr")].filter(r => !r.classList.contains("empty-row"));
  }

  function getCellElement(rIdx, cIdx) {
    const rows = getTableRows();
    if (rIdx < 0 || rIdx >= rows.length) return null;
    const cells = rows[rIdx].querySelectorAll("td");
    if (cIdx < 0 || cIdx >= cells.length) return null;
    return cells[cIdx];
  }

  function normalizeRange(r1, c1, r2, c2) {
    return {
      r1: Math.min(r1, r2),
      r2: Math.max(r1, r2),
      c1: Math.min(c1, c2),
      c2: Math.max(c1, c2)
    };
  }

  function updateRowIndices() {
    const rows = getTableRows();
    rows.forEach((tr, idx) => {
      tr.dataset.rowIndex = String(idx);
      const colIdx = tr.querySelector(".col-row-idx");
      if (colIdx) {
        colIdx.textContent = String(idx + 1);
        colIdx.title = `Linha ${idx + 1}`;
      }
    });
  }

  function renderSelection() {
    const tbody = el("pi-spreadsheet-body");
    if (!tbody) return;

    tbody.querySelectorAll(".cell-selected").forEach(el => el.classList.remove("cell-selected"));
    tbody.querySelectorAll(".cell-in-range").forEach(el => el.classList.remove("cell-in-range"));
    tbody.querySelectorAll(".range-top").forEach(el => el.classList.remove("range-top"));
    tbody.querySelectorAll(".range-bottom").forEach(el => el.classList.remove("range-bottom"));
    tbody.querySelectorAll(".range-left").forEach(el => el.classList.remove("range-left"));
    tbody.querySelectorAll(".range-right").forEach(el => el.classList.remove("range-right"));
    tbody.querySelectorAll(".cell-editing").forEach(el => el.classList.remove("cell-editing"));
    tbody.querySelectorAll("tr.row-selected").forEach(el => el.classList.remove("row-selected"));
    tbody.querySelectorAll(".grid-fill-handle").forEach(el => el.remove());

    if (!piActiveCell) {
      updateDeleteButtonState();
      updateGridStatusBar();
      return;
    }

    if (!piSelectionRange) {
      piSelectionRange = {
        r1: piActiveCell.rIdx,
        c1: piActiveCell.cIdx,
        r2: piActiveCell.rIdx,
        c2: piActiveCell.cIdx
      };
    }

    const rows = getTableRows();
    if (rows.length === 0) return;

    // Highlight active row & active cell
    const activeRow = rows[piActiveCell.rIdx];
    if (activeRow) {
      activeRow.classList.add("row-selected");
      const activeCell = activeRow.querySelectorAll("td")[piActiveCell.cIdx];
      if (activeCell) {
        activeCell.classList.add("cell-selected");
        if (piIsEditing) {
          activeCell.classList.add("cell-editing");
        }
      }
    }

    // Highlight selected range
    const { r1, c1, r2, c2 } = piSelectionRange;
    for (let r = r1; r <= r2 && r < rows.length; r++) {
      const row = rows[r];
      if (!row) continue;
      const cells = row.querySelectorAll("td");
      for (let c = c1; c <= c2 && c < cells.length; c++) {
        const td = cells[c];
        if (!td) continue;
        td.classList.add("cell-in-range");
        if (r === r1) td.classList.add("range-top");
        if (r === r2) td.classList.add("range-bottom");
        if (c === c1) td.classList.add("range-left");
        if (c === c2) td.classList.add("range-right");
      }
    }

    // Append Fill Handle to bottom-right of selection range (if contains editable columns)
    const canShowHandle = c1 >= 1 && c1 <= 4 && c2 <= 4;
    if (canShowHandle && r2 < rows.length && !piIsEditing) {
      const bottomRow = rows[r2];
      const handleCellCol = Math.min(c2, 4);
      const handleTd = bottomRow?.querySelectorAll("td")[handleCellCol];
      if (handleTd) {
        const handle = document.createElement("div");
        handle.className = "grid-fill-handle";
        handle.title = "Arraste para repetir valores";
        handle.addEventListener("mousedown", e => {
          e.stopPropagation();
          e.preventDefault();
          startFillHandleDrag();
        });
        handleTd.appendChild(handle);
      }
    }

    piSelectedCell = piActiveCell;
    updateDeleteButtonState();
    updateGridStatusBar();
  }

  function selectSingleCell(rIdx, cIdx) {
    if (piIsEditing) {
      commitCellEdit();
    }
    const rows = getTableRows();
    if (rows.length === 0) return;
    const clampedR = Math.max(0, Math.min(rows.length - 1, rIdx));
    const clampedC = Math.max(1, Math.min(9, cIdx));

    piActiveCell = { rIdx: clampedR, cIdx: clampedC };
    piSelectedCell = piActiveCell;
    piSelectionRange = { r1: clampedR, c1: clampedC, r2: clampedR, c2: clampedC };
    piIsEditing = false;
    renderSelection();

    const sheetTable = el("pi-spreadsheet-table");
    if (sheetTable && !piIsEditing) {
      try {
        sheetTable.focus({ preventScroll: true });
      } catch (_) {}
    }
  }

  function selectCell(rIdx, cIdx) {
    selectSingleCell(rIdx, cIdx);
  }

  function extendSelectionTo(rIdx, cIdx) {
    if (!piActiveCell) {
      selectSingleCell(rIdx, cIdx);
      return;
    }
    const rows = getTableRows();
    if (rows.length === 0) return;
    const clampedR = Math.max(0, Math.min(rows.length - 1, rIdx));

    if (piActiveCell.cIdx >= 1 && piActiveCell.cIdx <= 4) {
      const clampedC = Math.max(1, Math.min(4, cIdx));
      piSelectionRange = normalizeRange(piActiveCell.rIdx, piActiveCell.cIdx, clampedR, clampedC);
    } else {
      piSelectionRange = { r1: clampedR, c1: piActiveCell.cIdx, r2: clampedR, c2: piActiveCell.cIdx };
    }
    renderSelection();
  }

  function startCellEdit(replaceValue = false, initialChar = null) {
    if (!piActiveCell) return;
    const { rIdx, cIdx } = piActiveCell;
    if (cIdx < 1 || cIdx > 4) return; // Result columns are strictly read-only

    const td = getCellElement(rIdx, cIdx);
    if (!td) return;
    const input = td.querySelector("input");
    if (!input) return;

    if (input.type === "checkbox") {
      input.checked = !input.checked;
      setPiPendingChanges(true);
      validateGrid();
      renderSelection();
      return;
    }

    piIsEditing = true;
    piEditingCellVal = input.value;
    td.classList.add("cell-editing");

    if (replaceValue && initialChar !== null) {
      input.value = initialChar;
      setPiPendingChanges(true);
      validateGrid();
      input.focus();
      input.selectionStart = input.selectionEnd = input.value.length;
    } else {
      input.focus();
      input.select();
    }
  }

  function commitCellEdit() {
    if (!piIsEditing) return;
    piIsEditing = false;
    if (piActiveCell) {
      const td = getCellElement(piActiveCell.rIdx, piActiveCell.cIdx);
      if (td) td.classList.remove("cell-editing");
      const input = td ? td.querySelector("input") : null;
      if (input && input.value !== piEditingCellVal) {
        setPiPendingChanges(true);
        validateGrid();
      }
    }
    renderSelection();
  }

  function cancelCellEdit() {
    if (!piIsEditing) return;
    piIsEditing = false;
    if (piActiveCell && piEditingCellVal !== null) {
      const td = getCellElement(piActiveCell.rIdx, piActiveCell.cIdx);
      const input = td ? td.querySelector("input") : null;
      if (input) {
        input.value = String(piEditingCellVal);
        validateGrid();
      }
      if (td) td.classList.remove("cell-editing");
    }
    renderSelection();
  }

  // --- FILL HANDLE ENGINE (Alça de Preenchimento por Arrasto) ---

  function startFillHandleDrag() {
    if (!piSelectionRange) return;
    piIsDraggingHandle = true;
    piDragHandleSourceRange = { ...piSelectionRange };
    piDragTargetRow = piSelectionRange.r2;
  }

  function onFillHandleHoverRow(rIdx) {
    if (!piIsDraggingHandle || !piDragHandleSourceRange) return;
    piDragTargetRow = Math.max(rIdx, piDragHandleSourceRange.r2);

    const rows = getTableRows();
    rows.forEach((tr, idx) => {
      tr.querySelectorAll("td.cell-drag-fill-preview").forEach(td => td.classList.remove("cell-drag-fill-preview"));
      if (idx > piDragHandleSourceRange.r2 && idx <= piDragTargetRow) {
        const cells = tr.querySelectorAll("td");
        const c1 = piDragHandleSourceRange.c1;
        const c2 = Math.min(piDragHandleSourceRange.c2, 4);
        for (let c = c1; c <= c2; c++) {
          if (cells[c]) cells[c].classList.add("cell-drag-fill-preview");
        }
      }
    });
  }

  function finishFillHandleDrag() {
    if (!piIsDraggingHandle) return;
    piIsDraggingHandle = false;

    const tbody = el("pi-spreadsheet-body");
    if (tbody) {
      tbody.querySelectorAll(".cell-drag-fill-preview").forEach(td => td.classList.remove("cell-drag-fill-preview"));
    }

    if (piDragHandleSourceRange && piDragTargetRow !== null && piDragTargetRow > piDragHandleSourceRange.r2) {
      const src = piDragHandleSourceRange;
      const targetR = piDragTargetRow;

      let domRows = getTableRows();
      while (domRows.length <= targetR) {
        addPiRowSilently();
        domRows = getTableRows();
      }

      const sourceRowCount = src.r2 - src.r1 + 1;
      const editC1 = src.c1;
      const editC2 = Math.min(src.c2, 4);

      for (let r = src.r2 + 1; r <= targetR; r++) {
        const patternOffset = (r - (src.r2 + 1)) % sourceRowCount;
        const srcRowIdx = src.r1 + patternOffset;
        const srcRow = domRows[srcRowIdx];
        const tgtRow = domRows[r];
        if (!srcRow || !tgtRow) continue;

        for (let c = editC1; c <= editC2; c++) {
          const tgtCell = tgtRow.querySelectorAll("td")[c];
          if (tgtCell) tgtCell.dataset.touched = "true";
          if (c === 1) {
            const srcInp = srcRow.querySelector(".opc-path");
            const tgtInp = tgtRow.querySelector(".opc-path");
            if (srcInp && tgtInp) tgtInp.value = srcInp.value;
          } else if (c === 2) {
            const srcInp = srcRow.querySelector(".pi-point");
            const tgtInp = tgtRow.querySelector(".pi-point");
            if (srcInp && tgtInp) tgtInp.value = srcInp.value;
          } else if (c === 3) {
            const srcInp = srcRow.querySelector(".publish-interval");
            const tgtInp = tgtRow.querySelector(".publish-interval");
            if (srcInp && tgtInp) tgtInp.value = srcInp.value;
          } else if (c === 4) {
            const srcInp = srcRow.querySelector(".row-enabled");
            const tgtInp = tgtRow.querySelector(".row-enabled");
            if (srcInp && tgtInp) tgtInp.checked = srcInp.checked;
          }
        }
      }

      setPiPendingChanges(true);
      validateGrid();
      piSelectionRange = { r1: src.r1, c1: src.c1, r2: targetR, c2: src.c2 };
      renderSelection();
      message("Preenchimento por arrasto concluído.", "info");
    }

    piDragHandleSourceRange = null;
    piDragTargetRow = null;
  }

  // --- CLIPBOARD ENGINE: COPY, CUT, PASTE ---

  function copySelectionToClipboard() {
    if (!piSelectionRange) return "";
    const { r1, c1, r2, c2 } = piSelectionRange;
    const domRows = getTableRows();
    const lines = [];

    for (let r = r1; r <= r2 && r < domRows.length; r++) {
      const row = domRows[r];
      if (!row) continue;
      const cells = row.querySelectorAll("td");
      const rowVals = [];
      for (let c = c1; c <= c2 && c < cells.length; c++) {
        const td = cells[c];
        if (!td) { rowVals.push(""); continue; }
        if (c >= 1 && c <= 3) {
          const inp = td.querySelector("input");
          rowVals.push(inp ? inp.value : "");
        } else if (c === 4) {
          const inp = td.querySelector("input");
          rowVals.push(inp ? (inp.checked ? "true" : "false") : "");
        } else {
          rowVals.push(td.textContent.trim());
        }
      }
      lines.push(rowVals.join("\t"));
    }

    const tsv = lines.join("\n");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(tsv).then(() => {
        message("Células copiadas para a área de transferência.", "info");
      }).catch(() => {});
    }
    return tsv;
  }

  function cutSelection() {
    if (!piSelectionRange) return;
    copySelectionToClipboard();

    const { r1, c1, r2, c2 } = piSelectionRange;
    const domRows = getTableRows();

    for (let r = r1; r <= r2 && r < domRows.length; r++) {
      const row = domRows[r];
      if (!row) continue;
      const cells = row.querySelectorAll("td");
      for (let c = c1; c <= c2 && c < cells.length; c++) {
        if (c === 1 || c === 2) {
          const inp = cells[c]?.querySelector("input");
          if (inp) inp.value = "";
        } else if (c === 3) {
          const inp = cells[c]?.querySelector("input");
          if (inp) inp.value = "5000";
        } else if (c === 4) {
          const inp = cells[c]?.querySelector("input");
          if (inp) inp.checked = false;
        }
      }
    }

    setPiPendingChanges(true);
    validateGrid();
    renderSelection();
    message("Células recortadas.", "info");
  }

  function handleTabularPaste(e) {
    const clipboardData = e.clipboardData || window.clipboardData;
    if (!clipboardData) return;
    const text = clipboardData.getData("text");
    if (!text) return;

    const hasTab = text.includes("\t");
    const hasNewline = text.includes("\n") || text.includes("\r");

    if (!hasTab && !hasNewline && piIsEditing) {
      setTimeout(() => {
        setPiPendingChanges(true);
        validateGrid();
      }, 0);
      return;
    }

    e.preventDefault();
    const lines = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
    if (lines.length > 0 && lines[lines.length - 1] === "") {
      lines.pop();
    }
    if (lines.length === 0) return;

    const matrix = lines.map(line => line.split("\t"));
    const tbody = el("pi-spreadsheet-body");
    if (!tbody) return;

    const EDITABLE_COLS = [1, 2, 3, 4];
    let startRow = piActiveCell ? piActiveCell.rIdx : 0;
    let startCol = piActiveCell ? piActiveCell.cIdx : 1;

    let colOffset = EDITABLE_COLS.indexOf(startCol);
    if (colOffset === -1) {
      colOffset = 0;
    }

    let domRows = getTableRows();
    const neededRows = startRow + matrix.length;

    while (domRows.length < neededRows) {
      addPiRowSilently();
      domRows = getTableRows();
    }

    updateRowIndices();
    updateRowCountBadge();

    matrix.forEach((rowVals, rIdxOffset) => {
      const targetRowIdx = startRow + rIdxOffset;
      const tr = domRows[targetRowIdx];
      if (!tr) return;

      rowVals.forEach((val, cIdxOffset) => {
        const targetEditColIdx = colOffset + cIdxOffset;
        if (targetEditColIdx >= EDITABLE_COLS.length) {
          // Ignore result columns (5..9)!
          return;
        }
        const actualColIdx = EDITABLE_COLS[targetEditColIdx];
        const targetTd = tr.querySelectorAll("td")[actualColIdx];
        if (targetTd) targetTd.dataset.touched = "true";
        const trimmed = val.trim();

        if (actualColIdx === 1) {
          const inp = tr.querySelector(".opc-path");
          if (inp) inp.value = trimmed;
        } else if (actualColIdx === 2) {
          const inp = tr.querySelector(".pi-point");
          if (inp) inp.value = trimmed;
        } else if (actualColIdx === 3) {
          const inp = tr.querySelector(".publish-interval");
          if (inp) {
            const num = parseInt(trimmed, 10);
            inp.value = isNaN(num) ? trimmed : String(num);
          }
        } else if (actualColIdx === 4) {
          const inp = tr.querySelector(".row-enabled");
          if (inp) {
            inp.checked = normalizeBoolean(trimmed, inp.checked);
          }
        }
      });
    });

    setPiPendingChanges(true);
    validateGrid();
    const endCol = Math.min(4, EDITABLE_COLS[colOffset] + matrix[0].length - 1);
    piSelectionRange = {
      r1: startRow,
      c1: EDITABLE_COLS[colOffset],
      r2: startRow + matrix.length - 1,
      c2: endCol
    };
    renderSelection();
    message(`${matrix.length} linha(s) colada(s) com sucesso na planilha.`, "info");
  }

  function normalizeBoolean(raw, fallback = true) {
    if (typeof raw === "boolean") return raw;
    const s = String(raw || "").trim().toLowerCase();
    if (["true", "1", "sim", "s", "yes", "y", "t", "verdadeiro", "v", "ativo", "habilitado"].includes(s)) {
      return true;
    }
    if (["false", "0", "não", "nao", "n", "no", "f", "falso", "inativo", "desabilitado"].includes(s)) {
      return false;
    }
    return fallback;
  }

  function handleCellKeyDown(e, rIdx, cIdx) {
    if (e.defaultPrevented) return;
    const rows = getTableRows();
    const totalRows = rows.length;

    // Clipboard shortcuts: Ctrl/Cmd + C, X
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "c" && !piIsEditing) {
      e.preventDefault();
      copySelectionToClipboard();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "x" && !piIsEditing) {
      e.preventDefault();
      cutSelection();
      return;
    }

    if (e.key === "Tab") {
      e.preventDefault();
      commitCellEdit();
      if (e.shiftKey) {
        if (cIdx === 1) {
          if (rIdx > 0) selectSingleCell(rIdx - 1, 4);
        } else if (cIdx === 2) {
          selectSingleCell(rIdx, 1);
        } else if (cIdx === 3) {
          selectSingleCell(rIdx, 2);
        } else if (cIdx === 4) {
          selectSingleCell(rIdx, 3);
        } else {
          selectSingleCell(rIdx, 4);
        }
      } else {
        if (cIdx === 1) {
          selectSingleCell(rIdx, 2);
        } else if (cIdx === 2) {
          selectSingleCell(rIdx, 3);
        } else if (cIdx === 3) {
          selectSingleCell(rIdx, 4);
        } else if (cIdx === 4) {
          if (rIdx < totalRows - 1) selectSingleCell(rIdx + 1, 1);
        } else {
          selectSingleCell(rIdx, 1);
        }
      }
      return;
    }

    if (e.key === "Enter") {
      e.preventDefault();
      if (piIsEditing) {
        commitCellEdit();
        if (e.shiftKey) {
          if (rIdx > 0) selectSingleCell(rIdx - 1, cIdx);
        } else {
          if (rIdx < totalRows - 1) selectSingleCell(rIdx + 1, cIdx);
        }
      } else {
        if (e.shiftKey) {
          if (rIdx > 0) selectSingleCell(rIdx - 1, cIdx);
        } else {
          if (cIdx >= 1 && cIdx <= 4) {
            startCellEdit(false);
          } else {
            if (rIdx < totalRows - 1) selectSingleCell(rIdx + 1, cIdx);
          }
        }
      }
      return;
    }

    if (e.key === "Escape") {
      e.preventDefault();
      cancelCellEdit();
      return;
    }

    if (e.key === "Delete" || e.key === "Backspace") {
      if (!piIsEditing) {
        e.preventDefault();
        if (piSelectionRange) {
          for (let r = piSelectionRange.r1; r <= piSelectionRange.r2 && r < rows.length; r++) {
            const row = rows[r];
            if (!row) continue;
            const cells = row.querySelectorAll("td");
            for (let c = piSelectionRange.c1; c <= piSelectionRange.c2 && c < cells.length; c++) {
              if (c === 1 || c === 2) {
                const inp = cells[c]?.querySelector("input");
                if (inp) inp.value = "";
              } else if (c === 3) {
                const inp = cells[c]?.querySelector("input");
                if (inp) inp.value = "5000";
              } else if (c === 4) {
                const inp = cells[c]?.querySelector("input");
                if (inp) inp.checked = false;
              }
            }
          }
          setPiPendingChanges(true);
          validateGrid();
          renderSelection();
        }
        return;
      }
    }

    if (e.key === "ArrowUp") {
      if (!piIsEditing) {
        e.preventDefault();
        if (e.shiftKey && piSelectionRange) {
          const newR1 = Math.max(0, piSelectionRange.r1 - 1);
          piSelectionRange = { ...piSelectionRange, r1: newR1 };
          renderSelection();
        } else {
          if (rIdx > 0) selectSingleCell(rIdx - 1, cIdx);
        }
        return;
      }
    }

    if (e.key === "ArrowDown") {
      if (!piIsEditing) {
        e.preventDefault();
        if (e.shiftKey && piSelectionRange) {
          const newR2 = Math.min(totalRows - 1, piSelectionRange.r2 + 1);
          piSelectionRange = { ...piSelectionRange, r2: newR2 };
          renderSelection();
        } else {
          if (rIdx < totalRows - 1) selectSingleCell(rIdx + 1, cIdx);
        }
        return;
      }
    }

    if (e.key === "ArrowLeft") {
      if (!piIsEditing) {
        e.preventDefault();
        if (e.shiftKey && piSelectionRange) {
          const newC1 = Math.max(1, piSelectionRange.c1 - 1);
          piSelectionRange = { ...piSelectionRange, c1: newC1 };
          renderSelection();
        } else {
          if (cIdx > 1) selectSingleCell(rIdx, cIdx - 1);
        }
        return;
      }
    }

    if (e.key === "ArrowRight") {
      if (!piIsEditing) {
        e.preventDefault();
        if (e.shiftKey && piSelectionRange) {
          const maxCol = piSelectionRange.c1 <= 4 ? 4 : 9;
          const newC2 = Math.min(maxCol, piSelectionRange.c2 + 1);
          piSelectionRange = { ...piSelectionRange, c2: newC2 };
          renderSelection();
        } else {
          if (cIdx < 9) selectSingleCell(rIdx, cIdx + 1);
        }
        return;
      }
    }

    // Direct typing on selected editable cell
    if (!piIsEditing && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (cIdx >= 1 && cIdx <= 3) {
        e.preventDefault();
        startCellEdit(true, e.key);
      }
    }
  }

  function updateGridStatusBar() {
    const bar = el("pi-grid-cell-msg");
    if (!bar) return;

    const colNames = [
      "#",
      "Endereço OPC",
      "PI Point",
      "Velocidade (ms)",
      "Ativo",
      "Último valor",
      "Qualidade",
      "Último timestamp",
      "Resultado",
      "Ações"
    ];

    const parts = [];

    if (piActiveCell) {
      const { rIdx, cIdx } = piActiveCell;
      if (piSelectionRange && (piSelectionRange.r1 !== piSelectionRange.r2 || piSelectionRange.c1 !== piSelectionRange.c2)) {
        const rowCount = piSelectionRange.r2 - piSelectionRange.r1 + 1;
        const colCount = piSelectionRange.c2 - piSelectionRange.c1 + 1;
        parts.push(`Intervalo: ${rowCount} linhas × ${colCount} colunas`);
      } else {
        parts.push(`Linha ${rIdx + 1} · ${colNames[cIdx] || cIdx}`);
      }

      const cellErrKey = `${rIdx}:${cIdx}`;
      const cellErr = piCellErrors.get(cellErrKey);
      if (cellErr) {
        parts.push(`<span class="status-error">${escapeHtml(cellErr)}</span>`);
      }
    }

    if (piCellErrors.size > 0) {
      if (!piActiveCell || !piCellErrors.has(`${piActiveCell.rIdx}:${piActiveCell.cIdx}`)) {
        parts.push(`<span class="status-error">${piCellErrors.size} célula(s) com erro</span>`);
      }
    } else {
      parts.push(`<span>Pronto</span>`);
    }

    if (piHasPendingChanges) {
      const rows = getTableRows();
      let modCount = 0;
      rows.forEach(r => {
        modCount += r.querySelectorAll("td.cell-modified").length;
      });
      if (modCount > 0) {
        parts.push(`<span style="color: #92400e; font-weight: 500;">${modCount} alterações pendentes</span>`);
      } else {
        parts.push(`<span style="color: #92400e; font-weight: 500;">Alterações pendentes</span>`);
      }
    }

    bar.innerHTML = parts.join(" &nbsp;·&nbsp; ");
  }

  function validateGrid() {
    const tbody = el("pi-spreadsheet-body");
    piCellErrors.clear();
    if (!tbody) return { isValid: true, errorCount: 0 };

    const rows = getTableRows();

    rows.forEach(r => {
      r.querySelectorAll("td.cell-invalid").forEach(td => td.classList.remove("cell-invalid"));
      r.querySelectorAll(".is-invalid").forEach(inp => inp.classList.remove("is-invalid"));
      r.querySelectorAll("td.cell-modified").forEach(td => td.classList.remove("cell-modified"));
    });

    const seenOpc = new Map();
    const seenPoints = new Map();
    let realErrorCount = 0;

    rows.forEach((r, rIdx) => {
      const cells = r.querySelectorAll("td");
      const tdOpc = cells[1];
      const tdPoint = cells[2];
      const tdInterval = cells[3];
      const tdEn = cells[4];

      const opcInput = tdOpc ? tdOpc.querySelector(".opc-path") : null;
      const pointInput = tdPoint ? tdPoint.querySelector(".pi-point") : null;
      const intervalInput = tdInterval ? tdInterval.querySelector(".publish-interval") : null;
      const enInput = tdEn ? tdEn.querySelector(".row-enabled") : null;

      const opcPath = opcInput ? opcInput.value.trim() : "";
      const pointName = pointInput ? pointInput.value.trim() : "";
      const intervalVal = intervalInput ? parseInt(intervalInput.value, 10) : NaN;

      // Track modified state (against initial loaded value)
      if (opcInput && opcInput.dataset.initialVal !== undefined && opcInput.value !== opcInput.dataset.initialVal) {
        tdOpc.classList.add("cell-modified");
      }
      if (pointInput && pointInput.dataset.initialVal !== undefined && pointInput.value !== pointInput.dataset.initialVal) {
        tdPoint.classList.add("cell-modified");
      }
      if (intervalInput && intervalInput.dataset.initialVal !== undefined && intervalInput.value !== intervalInput.dataset.initialVal) {
        tdInterval.classList.add("cell-modified");
      }
      if (enInput && enInput.dataset.initialVal !== undefined && String(enInput.checked) !== enInput.dataset.initialVal) {
        tdEn.classList.add("cell-modified");
      }

      if (!opcPath) {
        realErrorCount++;
        if (tdOpc && tdOpc.dataset.touched === "true") {
          tdOpc.classList.add("cell-invalid");
          if (opcInput) opcInput.classList.add("is-invalid");
          piCellErrors.set(`${rIdx}:1`, `Linha ${rIdx + 1}: Endereço OPC obrigatório.`);
        }
      } else {
        if (!seenOpc.has(opcPath)) {
          seenOpc.set(opcPath, []);
        }
        seenOpc.get(opcPath).push({ rIdx, td: tdOpc, inp: opcInput });
      }

      if (!pointName) {
        realErrorCount++;
        if (tdPoint && tdPoint.dataset.touched === "true") {
          tdPoint.classList.add("cell-invalid");
          if (pointInput) pointInput.classList.add("is-invalid");
          piCellErrors.set(`${rIdx}:2`, `Linha ${rIdx + 1}: PI Point obrigatório.`);
        }
      } else {
        const ptKey = pointName.toUpperCase();
        if (!seenPoints.has(ptKey)) {
          seenPoints.set(ptKey, []);
        }
        seenPoints.get(ptKey).push({ rIdx, td: tdPoint, inp: pointInput, name: pointName });
      }

      if (isNaN(intervalVal) || intervalVal < 1000 || intervalVal > 60000) {
        realErrorCount++;
        if (tdInterval && tdInterval.dataset.touched === "true") {
          tdInterval.classList.add("cell-invalid");
          if (intervalInput) intervalInput.classList.add("is-invalid");
          piCellErrors.set(`${rIdx}:3`, `Linha ${rIdx + 1}: Velocidade de publicação deve ser um número inteiro entre 1000 e 60000 ms.`);
        }
      }
    });

    for (const [path, occurrences] of seenOpc.entries()) {
      if (occurrences.length > 1) {
        realErrorCount += (occurrences.length - 1);
        occurrences.forEach(item => {
          if (item.td && item.td.dataset.touched === "true") {
            item.td.classList.add("cell-invalid");
            if (item.inp) item.inp.classList.add("is-invalid");
            piCellErrors.set(`${item.rIdx}:1`, `Linha ${item.rIdx + 1}: Endereço OPC "${path}" duplicado na planilha.`);
          }
        });
      }
    }

    for (const [ptKey, occurrences] of seenPoints.entries()) {
      if (occurrences.length > 1) {
        realErrorCount += (occurrences.length - 1);
        occurrences.forEach(item => {
          if (item.td && item.td.dataset.touched === "true") {
            item.td.classList.add("cell-invalid");
            if (item.inp) item.inp.classList.add("is-invalid");
            piCellErrors.set(`${item.rIdx}:2`, `Linha ${item.rIdx + 1}: PI Point "${item.name}" duplicado na planilha.`);
          }
        });
      }
    }

    const isValid = realErrorCount === 0;
    updateSaveButtonState(isValid);
    updateGridStatusBar();

    return { isValid, errorCount: realErrorCount };
  }

  function setPiPendingChanges(dirty) {
    piHasPendingChanges = Boolean(dirty);
    const badge = el("pi-pending-changes-badge");
    if (badge) {
      badge.hidden = !piHasPendingChanges;
    }
    updateSaveButtonState();
    updateGridStatusBar();
  }

  function updateSaveButtonState(isValid) {
    const btnSave = el("btn-save-pi-sheet");
    if (!btnSave) return;

    const valid = isValid !== undefined ? isValid : (piCellErrors.size === 0);
    const rows = getTableRows();

    const canSave = Boolean(
      selectedPiEquipment &&
      selectedPiProgId &&
      currentPiProfile &&
      currentPiProfile.enabled &&
      piHasPendingChanges &&
      valid &&
      rows.length > 0
    );

    btnSave.disabled = !canSave;
  }

  function updateDeleteButtonState() {
    const btnDelete = el("btn-delete-pi-row");
    if (!btnDelete) return;
    const rows = getTableRows();
    const hasSelectedRow = piActiveCell !== null && piActiveCell.rIdx >= 0 && piActiveCell.rIdx < rows.length;
    btnDelete.disabled = !hasSelectedRow;
  }

  function renderPiSpreadsheet() {
    const tbody = el("pi-spreadsheet-body");
    const countBadge = el("pi-mapping-count-badge");
    if (!tbody) return;

    tbody.replaceChildren();

    if (!selectedPiEquipment || !selectedPiProgId) {
      const tr = document.createElement("tr");
      tr.innerHTML = '<td colspan="10" class="muted">Selecione um equipamento e informe o Servidor OPC para carregar a planilha.</td>';
      tbody.appendChild(tr);
      if (countBadge) countBadge.textContent = "0 linha(s)";
      piActiveCell = null;
      piSelectedCell = null;
      piSelectionRange = null;
      updateDeleteButtonState();
      updateSaveButtonState();
      return;
    }

    if (piMappings.length === 0) {
      const tr = document.createElement("tr");
      tr.className = "empty-row";
      tr.innerHTML = '<td colspan="10" class="muted">Nenhuma linha de mapeamento cadastrada. Clique em "+ Adicionar linha" para começar.</td>';
      tbody.appendChild(tr);
      if (countBadge) countBadge.textContent = "0 linha(s)";
      piActiveCell = null;
      piSelectedCell = null;
      piSelectionRange = null;
      updateDeleteButtonState();
      updateSaveButtonState();
      return;
    }

    if (countBadge) countBadge.textContent = `${piMappings.length} linha(s)`;

    piMappings.forEach((m, idx) => {
      const tr = createSpreadsheetRowElement(m, idx);
      tbody.appendChild(tr);
    });

    updateRowIndices();
    validateGrid();
    updateDeleteButtonState();
    if (piMappings.length > 0) {
      selectSingleCell(0, 1);
    }
  }

  function createSpreadsheetRowElement(m, idx) {
    const tr = document.createElement("tr");
    tr.dataset.mappingId = m.mapping_id || "";
    tr.dataset.rowIndex = String(idx);

    // 0. # (Índice de linha sticky)
    const tdIdx = document.createElement("td");
    tdIdx.className = "col-row-idx";
    tdIdx.textContent = String(idx + 1);
    tdIdx.title = `Linha ${idx + 1}`;
    tdIdx.addEventListener("mousedown", e => {
      if (e.button !== 0) return;
      selectSingleCell(parseInt(tr.dataset.rowIndex, 10), 1);
    });
    tdIdx.addEventListener("mouseenter", () => {
      const curR = parseInt(tr.dataset.rowIndex, 10);
      if (piIsDraggingHandle) {
        onFillHandleHoverRow(curR);
      } else if (piIsMouseDown && piDragAnchor && piDragAnchor.cIdx <= 4) {
        piSelectionRange = normalizeRange(piDragAnchor.rIdx, piDragAnchor.cIdx, curR, 1);
        renderSelection();
      }
    });
    tr.appendChild(tdIdx);

    tr.addEventListener("mouseenter", () => {
      const curR = parseInt(tr.dataset.rowIndex, 10);
      if (piIsDraggingHandle) {
        onFillHandleHoverRow(curR);
      }
    });

    // Helper for editable td click & drag wiring
    function setupEditableTd(td, colIdx, input) {
      td.addEventListener("mousedown", e => {
        if (e.button !== 0) return;
        if (e.target.classList.contains("grid-fill-handle")) return;
        const curR = parseInt(tr.dataset.rowIndex, 10);
        if (e.shiftKey) {
          extendSelectionTo(curR, colIdx);
        } else {
          piIsMouseDown = true;
          piMouseDownPos = { x: e.clientX, y: e.clientY };
          piIsDraggingSelection = false;
          piDragAnchor = { rIdx: curR, cIdx: colIdx };
          selectSingleCell(curR, colIdx);
        }
      });
      td.addEventListener("mouseenter", e => {
        const curR = parseInt(tr.dataset.rowIndex, 10);
        if (piIsDraggingHandle) {
          onFillHandleHoverRow(curR);
        } else if (piIsMouseDown && piDragAnchor && piDragAnchor.cIdx <= 4) {
          if (!piIsDraggingSelection && piMouseDownPos && e) {
            const dist = Math.hypot(e.clientX - piMouseDownPos.x, e.clientY - piMouseDownPos.y);
            if (dist < 6) return;
            piIsDraggingSelection = true;
          }
          const clampedCol = Math.max(1, Math.min(4, colIdx));
          piSelectionRange = normalizeRange(piDragAnchor.rIdx, piDragAnchor.cIdx, curR, clampedCol);
          renderSelection();
        }
      });
      td.addEventListener("dblclick", () => {
        startCellEdit(false);
      });
      input.addEventListener("blur", () => {
        td.dataset.touched = "true";
        if (piIsEditing) {
          commitCellEdit();
        }
      });
      input.addEventListener("keydown", e => handleCellKeyDown(e, parseInt(tr.dataset.rowIndex, 10), colIdx));
      input.addEventListener("input", () => {
        td.dataset.touched = "true";
        setPiPendingChanges(true);
        validateGrid();
      });
      input.addEventListener("change", () => {
        td.dataset.touched = "true";
        setPiPendingChanges(true);
        validateGrid();
      });
    }

    // 1. Endereço OPC (editável)
    const tdOpc = document.createElement("td");
    tdOpc.className = "cell-editable col-opc";
    const inputOpc = document.createElement("input");
    inputOpc.type = "text";
    inputOpc.className = "grid-cell-input opc-path";
    inputOpc.value = m.opc_item_path || "";
    inputOpc.placeholder = "Canal.Dispositivo.Tag";
    inputOpc.required = true;
    inputOpc.dataset.initialVal = m.opc_item_path || "";
    tdOpc.appendChild(inputOpc);
    setupEditableTd(tdOpc, 1, inputOpc);
    tr.appendChild(tdOpc);

    // 2. PI Point (editável)
    const tdPt = document.createElement("td");
    tdPt.className = "cell-editable col-pi-point";
    const inputPt = document.createElement("input");
    inputPt.type = "text";
    inputPt.className = "grid-cell-input pi-point";
    inputPt.value = m.pi_point_name || "";
    inputPt.placeholder = "PI_Point_Tag";
    inputPt.required = true;
    inputPt.dataset.initialVal = m.pi_point_name || "";
    tdPt.appendChild(inputPt);
    setupEditableTd(tdPt, 2, inputPt);
    tr.appendChild(tdPt);

    // 3. Velocidade de publicação (ms) (editável)
    const tdInt = document.createElement("td");
    tdInt.className = "cell-editable col-interval";
    const inputInt = document.createElement("input");
    inputInt.type = "number";
    inputInt.className = "grid-cell-input publish-interval";
    inputInt.min = "1000";
    inputInt.max = "60000";
    inputInt.step = "1";
    inputInt.value = m.publish_interval_ms || 5000;
    inputInt.required = true;
    inputInt.dataset.initialVal = String(m.publish_interval_ms || 5000);
    tdInt.appendChild(inputInt);
    setupEditableTd(tdInt, 3, inputInt);
    tr.appendChild(tdInt);

    // 4. Ativo (checkbox editável)
    const tdEn = document.createElement("td");
    tdEn.className = "cell-editable col-active";
    const inputEn = document.createElement("input");
    inputEn.type = "checkbox";
    inputEn.className = "grid-cell-checkbox row-enabled";
    inputEn.checked = m.enabled !== undefined ? Boolean(m.enabled) : true;
    inputEn.dataset.initialVal = String(m.enabled !== undefined ? Boolean(m.enabled) : true);
    tdEn.appendChild(inputEn);
    setupEditableTd(tdEn, 4, inputEn);
    tr.appendChild(tdEn);

    // Persisted rows loaded from server start with touched=true
    if (m.mapping_id) {
      tdOpc.dataset.touched = "true";
      tdPt.dataset.touched = "true";
      tdInterval.dataset.touched = "true";
      tdEn.dataset.touched = "true";
    }

    // Helper for readonly result columns
    function setupReadonlyTd(td, colIdx) {
      td.addEventListener("mousedown", e => {
        if (e.button !== 0) return;
        const curR = parseInt(tr.dataset.rowIndex, 10);
        piIsMouseDown = false;
        piDragAnchor = null;
        selectSingleCell(curR, colIdx);
      });
      td.addEventListener("mouseenter", () => {
        const curR = parseInt(tr.dataset.rowIndex, 10);
        if (piIsDraggingHandle) {
          onFillHandleHoverRow(curR);
        } else if (piIsMouseDown && piDragAnchor && piDragAnchor.cIdx <= 4) {
          piSelectionRange = normalizeRange(piDragAnchor.rIdx, piDragAnchor.cIdx, curR, 4);
          renderSelection();
        }
      });
    }

    // 5. Último valor (somente leitura)
    const tdVal = document.createElement("td");
    tdVal.className = "cell-readonly col-val";
    tdVal.textContent = m.current_value !== null && m.current_value !== undefined ? String(m.current_value) : "—";
    setupReadonlyTd(tdVal, 5);
    tr.appendChild(tdVal);

    // 6. Qualidade (somente leitura)
    const tdQual = document.createElement("td");
    tdQual.className = "cell-readonly col-qual";
    if (m.quality === null || m.quality === undefined) {
      tdQual.textContent = "—";
    } else if (m.quality >= 192) {
      tdQual.innerHTML = '<span class="cell-quality good">Good</span>';
    } else {
      tdQual.innerHTML = `<span class="cell-quality bad">Bad (${m.quality})</span>`;
    }
    setupReadonlyTd(tdQual, 6);
    tr.appendChild(tdQual);

    // 7. Último timestamp (somente leitura)
    const tdTs = document.createElement("td");
    tdTs.className = "cell-readonly col-ts";
    tdTs.textContent = formatOpcTimestamp(m.opc_timestamp);
    setupReadonlyTd(tdTs, 7);
    tr.appendChild(tdTs);

    // 8. Resultado (somente leitura)
    const tdRes = document.createElement("td");
    tdRes.className = "cell-readonly col-res";
    const st = String(m.last_publish_status || "").toLowerCase();
    if (st === "simulado" || st === "simulated") {
      const tsFormatted = formatOpcTimestamp(m.last_published_at);
      tdRes.innerHTML = `<span class="badge simulated" title="Valor: ${m.last_published_value || ''}">Simulado (${tsFormatted})</span>`;
    } else if (st === "publicado" || st === "published") {
      const tsFormatted = formatOpcTimestamp(m.last_published_at);
      tdRes.innerHTML = `<span class="badge good" title="Valor: ${m.last_published_value || ''}">Publicado (${tsFormatted})</span>`;
    } else if (st === "erro" || st === "error") {
      tr.classList.add("row-error");
      const errTxt = escapeHtml(m.last_publish_error || "Erro na publicação");
      tdRes.innerHTML = `<span class="badge error" title="${errTxt}">Erro</span><small class="error-msg" style="display:block; font-size:11px; color:var(--status-red); margin-top:2px;" title="${errTxt}">${errTxt}</small>`;
    } else if (st === "desabilitado" || st === "disabled") {
      tdRes.innerHTML = '<span class="badge muted">Desabilitado</span>';
    } else if (st === "lido via opc") {
      tdRes.innerHTML = '<span class="badge info">Lido via OPC</span>';
    } else {
      tdRes.innerHTML = '<span class="badge unconfigured">Não configurado</span>';
    }
    setupReadonlyTd(tdRes, 8);
    tr.appendChild(tdRes);

    // 9. Ações (somente leitura)
    const tdAct = document.createElement("td");
    tdAct.className = "cell-actions actions";
    tdAct.style.justifyContent = "center";

    if (m.mapping_id) {
      const btnValidate = document.createElement("button");
      btnValidate.type = "button";
      btnValidate.className = "btn-sm";
      btnValidate.textContent = "Validar";
      btnValidate.title = "Validar se o PI Point existe e se Point Source e Location1 coincidem com o perfil ativo";
      btnValidate.addEventListener("click", () => validatePiPointMapping(m.mapping_id));
      tdAct.appendChild(btnValidate);

      const btnPubOnce = document.createElement("button");
      btnPubOnce.type = "button";
      btnPubOnce.className = "btn-sm primary btn-publish-once";
      btnPubOnce.textContent = "Publicar uma vez";
      if (!piOutputEnabled) {
        btnPubOnce.disabled = true;
        btnPubOnce.title = "Publicação desabilitada: saída PI desabilitada externamente (OPC_BRIDGE_PI_OUTPUT_ENABLED=true).";
      } else {
        btnPubOnce.disabled = !m.enabled;
        btnPubOnce.title = "Publicar a leitura atual em cache no PI Point de destino";
        btnPubOnce.addEventListener("click", () => openPublishOnceModal(tr, m));
      }
      tdAct.appendChild(btnPubOnce);

      const btnSim = document.createElement("button");
      btnSim.type = "button";
      btnSim.className = "btn-sm";
      btnSim.textContent = "Simular";
      btnSim.title = "Simular publicação no PI usando exclusivamente o valor em cache";
      btnSim.disabled = !m.enabled;
      btnSim.addEventListener("click", () => simulatePiMapping(m.mapping_id));
      tdAct.appendChild(btnSim);

      const btnDel = document.createElement("button");
      btnDel.type = "button";
      btnDel.className = "btn-sm danger btn-delete-row";
      btnDel.textContent = "Excluir linha";
      btnDel.title = "Excluir esta linha de mapeamento";
      btnDel.addEventListener("click", () => confirmDeleteRow(tr, m));
      tdAct.appendChild(btnDel);
    } else {
      const btnDel = document.createElement("button");
      btnDel.type = "button";
      btnDel.className = "btn-sm danger btn-delete-row";
      btnDel.textContent = "Excluir linha";
      btnDel.title = "Remover esta linha da planilha";
      btnDel.addEventListener("click", () => {
        tr.remove();
        updateRowIndices();
        updateRowCountBadge();
        const remaining = getTableRows();
        if (remaining.length === 0) {
          const emptyTr = document.createElement("tr");
          emptyTr.className = "empty-row";
          emptyTr.innerHTML = '<td colspan="10" class="muted">Nenhuma linha de mapeamento cadastrada. Clique em "+ Adicionar linha" para começar.</td>';
          tbody.appendChild(emptyTr);
          piActiveCell = null;
          piSelectedCell = null;
          piSelectionRange = null;
        }
        setPiPendingChanges(true);
        validateGrid();
        renderSelection();
      });
      tdAct.appendChild(btnDel);
    }

    tr.appendChild(tdAct);
    return tr;
  }

  function addPiRowSilently() {
    const tbody = el("pi-spreadsheet-body");
    if (!tbody) return null;
    const emptyRow = tbody.querySelector(".empty-row");
    if (emptyRow) emptyRow.remove();

    const newMapping = {
      mapping_id: "",
      equipment_id: selectedPiEquipment?.equipment_id || "",
      opc_prog_id: selectedPiProgId,
      opc_item_path: "",
      pi_point_name: "",
      publish_interval_ms: 5000,
      enabled: true,
      current_value: null,
      quality: null,
      quality_text: null,
      opc_timestamp: null,
      last_publish_status: "unconfigured"
    };

    const rows = getTableRows();
    const newIdx = rows.length;
    const tr = createSpreadsheetRowElement(newMapping, newIdx);
    tbody.appendChild(tr);
    updateRowIndices();
    updateRowCountBadge();
    return tr;
  }

  function addPiRow() {
    const tr = addPiRowSilently();
    if (!tr) return;
    const newIdx = parseInt(tr.dataset.rowIndex, 10);
    setPiPendingChanges(true);
    validateGrid();
    selectSingleCell(newIdx, 1);
    startCellEdit(false);
  }

  function deleteSelectedRow() {
    if (!piActiveCell) {
      message("Selecione uma linha na grade para excluir.", "warning");
      return;
    }
    const domRows = getTableRows();
    const tr = domRows[piActiveCell.rIdx];
    if (!tr) return;

    const mappingId = tr.dataset.mappingId || "";
    const piPointName = tr.querySelector(".pi-point")?.value || "";
    confirmDeleteRow(tr, { mapping_id: mappingId, pi_point_name: piPointName });
  }

  function confirmDeleteRow(tr, m) {
    if (!m || !m.mapping_id) {
      tr.remove();
      updateRowIndices();
      updateRowCountBadge();
      const tbody = el("pi-spreadsheet-body");
      const remaining = getTableRows();
      if (remaining.length === 0 && tbody) {
        const emptyTr = document.createElement("tr");
        emptyTr.className = "empty-row";
        emptyTr.innerHTML = '<td colspan="10" class="muted">Nenhuma linha de mapeamento cadastrada. Clique em "+ Adicionar linha" para começar.</td>';
        tbody.appendChild(emptyTr);
        piActiveCell = null;
        piSelectedCell = null;
        piSelectionRange = null;
      } else {
        const nextIdx = Math.min(piActiveCell ? piActiveCell.rIdx : 0, remaining.length - 1);
        selectSingleCell(nextIdx, 1);
      }
      setPiPendingChanges(true);
      validateGrid();
      updateDeleteButtonState();
      return;
    }
    deletingMappingId = m.mapping_id;
    const ptNameEl = el("delete-mapping-point-name");
    if (ptNameEl) ptNameEl.textContent = m.pi_point_name || "—";
    const modal = el("modal-delete-mapping-confirm");
    if (modal) modal.hidden = false;
  }

  async function executeDeleteMapping() {
    if (!deletingMappingId) return;
    try {
      await api(`/api/v1/pi-mappings/${encodeURIComponent(deletingMappingId)}`, undefined, "DELETE");
      message("Mapeamento excluído com sucesso.", "success");
      const modal = el("modal-delete-mapping-confirm");
      if (modal) modal.hidden = true;
      deletingMappingId = null;
      piActiveCell = null;
      piSelectedCell = null;
      piSelectionRange = null;
      updateDeleteButtonState();
      await loadPiMappings();
      await loadPiAudit();
    } catch (err) {
      message(err.message, "error");
    }
  }

  async function savePiSheetChanges() {
    clearSheetMessages();
    if (!selectedPiEquipment || !selectedPiProgId) {
      showSheetError("Selecione um equipamento e informe o Servidor OPC antes de salvar.");
      return;
    }
    if (!currentPiProfile || !currentPiProfile.enabled) {
      showSheetError("É obrigatório configurar e ativar o perfil PI para este Equipamento e Servidor OPC antes de salvar a planilha.");
      return;
    }

    // Mark ALL editable cells as touched on save attempt so any invalid/empty cell is revealed
    const rows = getTableRows();
    rows.forEach(r => {
      r.querySelectorAll("td.cell-editable").forEach(td => {
        td.dataset.touched = "true";
      });
    });

    const { isValid } = validateGrid();
    if (!isValid) {
      showSheetError("Existem células com erros na planilha. Corrija os campos destacados em vermelho antes de salvar.");
      message("Corrija os erros destacados na planilha antes de salvar.", "error");
      return;
    }

    const batchRows = rows.map(r => {
      const opcInput = r.querySelector(".opc-path");
      const pointInput = r.querySelector(".pi-point");
      const intervalInput = r.querySelector(".publish-interval");
      const enabledInput = r.querySelector(".row-enabled");

      return {
        mapping_id: r.dataset.mappingId || "",
        opc_item_path: opcInput ? opcInput.value.trim() : "",
        pi_point_name: pointInput ? pointInput.value.trim() : "",
        publish_interval_ms: intervalInput ? parseInt(intervalInput.value, 10) : 5000,
        enabled: enabledInput ? enabledInput.checked : true
      };
    });

    try {
      const btnSave = el("btn-save-pi-sheet");
      if (btnSave) btnSave.disabled = true;
      const payload = {
        equipment_id: selectedPiEquipment.equipment_id,
        opc_prog_id: selectedPiProgId,
        rows: batchRows
      };
      const res = await api("/api/v1/pi-mappings/batch", payload, "POST");
      showSheetSuccess(`${res.saved_count || batchRows.length} mapeamento(s) salvo(s) com sucesso.`);
      message("Planilha de mapeamentos PI salva com sucesso.", "success");

      const savedSelected = piActiveCell ? { ...piActiveCell } : null;
      setPiPendingChanges(false);
      await loadPiMappings();
      await loadPiAudit();

      if (savedSelected) {
        selectSingleCell(savedSelected.rIdx, savedSelected.cIdx);
      }
    } catch (err) {
      if (err.data && err.data.row_errors) {
        err.data.row_errors.forEach(re => {
          const r = rows[re.row_index];
          if (r) {
            const cells = r.querySelectorAll("td");
            if (re.field === "opc_item_path") {
              cells[1]?.classList.add("cell-invalid");
              r.querySelector(".opc-path")?.classList.add("is-invalid");
              piCellErrors.set(`${re.row_index}:1`, re.message);
            }
            if (re.field === "pi_point_name") {
              cells[2]?.classList.add("cell-invalid");
              r.querySelector(".pi-point")?.classList.add("is-invalid");
              piCellErrors.set(`${re.row_index}:2`, re.message);
            }
            if (re.field === "publish_interval_ms") {
              cells[3]?.classList.add("cell-invalid");
              r.querySelector(".publish-interval")?.classList.add("is-invalid");
              piCellErrors.set(`${re.row_index}:3`, re.message);
            }
          }
        });
        const msgs = err.data.row_errors.map(re => re.message).join("<br>");
        showSheetError(msgs);
        updateSaveButtonState(false);
        updateGridStatusBar();
      } else {
        showSheetError(err.message);
      }
      message(err.message, "error");
    } finally {
      updateSaveButtonState();
    }
  }

  async function readNowPiTags() {
    if (!selectedPiEquipment?.agent_id || busy) return;
    const btn = el("btn-read-now-pi");
    const tbody = el("pi-spreadsheet-body");
    if (!tbody) return;
    const rows = getTableRows();

    const tags = rows.map(r => r.querySelector(".opc-path")?.value.trim()).filter(Boolean);
    if (tags.length === 0) {
      message("Nenhum endereço OPC informado na planilha para leitura.", "warning");
      return;
    }

    try {
      busy = true;
      if (btn) btn.disabled = true;
      message("Lendo valores atuais no servidor OPC via agente...", "info");

      const payload = {
        equipment_id: selectedPiEquipment.equipment_id,
        opc_prog_id: selectedPiProgId,
        tags: tags
      };

      const data = await api("/api/v1/pi-integration/read-now", payload, "POST");
      const results = data.results || [];
      const notFoundList = [];

      rows.forEach(r => {
        const opcInput = r.querySelector(".opc-path");
        const path = opcInput ? opcInput.value.trim() : "";
        const res = results.find(resItem => resItem.opc_item_path === path);

        const tdVal = r.querySelector(".col-val");
        const tdQual = r.querySelector(".col-qual");
        const tdTs = r.querySelector(".col-ts");
        const tdRes = r.querySelector(".col-res");

        if (res) {
          if (res.status === "valid" || (res.value !== null && res.value !== undefined)) {
            opcInput.classList.remove("is-invalid");
            if (tdVal) tdVal.textContent = String(res.value);
            if (tdQual) {
              const q = res.quality !== null && res.quality !== undefined ? res.quality : 192;
              if (q >= 192) {
                tdQual.innerHTML = '<span class="cell-quality good">Good</span>';
              } else {
                tdQual.innerHTML = `<span class="cell-quality bad">Bad (${q})</span>`;
              }
            }
            if (tdTs) tdTs.textContent = formatOpcTimestamp(res.opc_timestamp);
            if (tdRes) tdRes.innerHTML = '<span class="badge info">Lido via OPC</span>';
          } else {
            opcInput.classList.add("is-invalid");
            if (tdVal) tdVal.textContent = "—";
            if (tdQual) {
              tdQual.innerHTML = `<span class="cell-quality bad" title="${escapeHtml(res.error || 'Endereço OPC não encontrado')}">Bad</span>`;
            }
            if (tdTs) tdTs.textContent = "—";
            if (tdRes) tdRes.innerHTML = '<span class="badge error">Falha leitura</span>';
            notFoundList.push(path);
          }
        }
      });

      if (notFoundList.length > 0) {
        message(`Leitura concluída com ${notFoundList.length} endereço(s) OPC com falha. Verifique os campos destacados em vermelho.`, "warning");
      } else {
        message(`Leitura concluída com sucesso para todas as ${tags.length} tags OPC.`, "success");
      }
    } catch (err) {
      message(err.message, "error");
    } finally {
      busy = false;
      updateSpreadsheetToolbar(Boolean(currentPiProfile && currentPiProfile.enabled));
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
      piOutputEnabled = Boolean(data.output_enabled);
      const banner = el("pi-simulation-banner");
      const btnTest = el("btn-test-pi-connection");
      if (banner) {
        if (data.output_enabled) {
          banner.className = "banner success";
          banner.textContent = data.banner_text || "Saída PI habilitada";
        } else {
          banner.className = "banner info";
          banner.textContent = data.banner_text || "Saída PI: desabilitada — nenhuma escrita real habilitada.";
        }
      }
      if (btnTest) {
        if (data.output_enabled) {
          btnTest.disabled = false;
          btnTest.title = "Testar conectividade com PI Web API";
        } else {
          btnTest.disabled = true;
          btnTest.title = "Saída PI desabilitada. Habilitação depende de configuração administrativa externa.";
        }
      }
      // Update any rendered row publication buttons
      const tbody = el("pi-spreadsheet-body");
      if (tbody) {
        tbody.querySelectorAll(".btn-publish-once").forEach(btn => {
          if (!piOutputEnabled) {
            btn.disabled = true;
            btn.title = "Publicação desabilitada: saída PI desabilitada externamente (OPC_BRIDGE_PI_OUTPUT_ENABLED=true).";
          } else {
            const tr = btn.closest("tr");
            const chk = tr ? tr.querySelector(".cell-chk-enabled") : null;
            btn.disabled = chk ? !chk.checked : false;
            btn.title = "Publicar a leitura atual em cache no PI Point de destino";
          }
        });
      }
    } catch (e) {
      // Ignore if unavailable
    }
  }

  async function testPiConnection() {
    if (!piOutputEnabled) {
      message("Saída PI desabilitada: teste de conexão bloqueado externamente.", "warning");
      return;
    }
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

  function openPublishOnceModal(trOrM, maybeM) {
    let tr = null;
    let m = null;
    if (maybeM) {
      tr = trOrM;
      m = maybeM;
    } else {
      m = trOrM;
    }

    let currentVal = m.current_value;
    let currentQual = m.quality_text || (m.quality !== null && m.quality !== undefined ? String(m.quality) : null);
    let currentTs = m.opc_timestamp;

    if (tr) {
      const valSpan = tr.querySelector(".col-val");
      const qualSpan = tr.querySelector(".col-qual");
      const tsSpan = tr.querySelector(".col-ts");
      if (valSpan && valSpan.textContent !== "—") currentVal = valSpan.textContent;
      if (qualSpan && qualSpan.textContent !== "—") currentQual = qualSpan.textContent;
      if (tsSpan && tsSpan.textContent !== "—") currentTs = tsSpan.textContent;
    }

    publishingOnceMapping = {
      ...m,
      current_value: currentVal,
      quality_text: currentQual,
      opc_timestamp: currentTs
    };

    const modal = el("modal-publish-once-confirm");
    if (!modal) return;

    if (el("publish-once-point")) el("publish-once-point").textContent = m.pi_point_name || "—";
    if (el("publish-once-tag")) el("publish-once-tag").textContent = m.opc_item_path || "—";
    if (el("publish-once-value")) el("publish-once-value").textContent = currentVal !== null && currentVal !== undefined ? String(currentVal) : "—";
    if (el("publish-once-quality")) el("publish-once-quality").textContent = currentQual || "—";
    if (el("publish-once-timestamp")) el("publish-once-timestamp").textContent = formatOpcTimestamp(currentTs);

    const warnBox = el("publish-once-warning");
    const confirmBtn = el("btn-confirm-publish-once");

    let blockReason = null;
    if (!piOutputEnabled) {
      blockReason = "Publicação bloqueada: saída PI desabilitada externamente (kill switch desligado).";
    } else if (currentVal === null || currentVal === undefined || currentVal === "—") {
      blockReason = "Publicação bloqueada: nenhum valor OPC coletado em cache para esta tag. Execute '⚡ Ler agora' antes.";
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

  function initTabs() {
    const tabEq = el("tab-equipments");
    const tabOpc = el("tab-opc");
    const tabPi = el("tab-pi");
    if (tabEq) tabEq.addEventListener("click", () => switchTab("equipments"));
    if (tabOpc) tabOpc.addEventListener("click", () => switchTab("opc"));
    if (tabPi) tabPi.addEventListener("click", () => switchTab("pi"));
  }

  function initEquipmentsModule() {
    const btnRef = el("btn-refresh-equipments");
    const btnShow = el("btn-show-add-equipment");
    const btnCancel = el("btn-cancel-equipment");
    const form = el("equipment-form");

    if (btnRef) btnRef.addEventListener("click", () => loadEquipments());
    if (btnShow) btnShow.addEventListener("click", showAddEquipmentForm);
    if (btnCancel) btnCancel.addEventListener("click", hideEquipmentForm);
    if (form) {
      form.addEventListener("submit", e => {
        e.preventDefault();
        saveEquipment();
      });
    }
  }

  function initOpcModule() {
    const btnRef = el("btn-refresh-opc");
    const selectEq = el("opc-equipment-select");
    const btnFind = el("find-servers");
    const servers = el("servers");
    const progId = el("prog-id");
    const interval = el("interval");
    const btnAddTag = el("add-tag");
    const btnValAll = el("validate-all");
    const btnSaveCfg = el("btn-save-config");
    const btnToggle = el("toggle-live");
    const btnApply = el("apply");
    const btnLoadAct = el("btn-load-active");

    if (btnRef) btnRef.addEventListener("click", () => onEquipmentSelected());
    if (selectEq) selectEq.addEventListener("change", onEquipmentSelected);
    if (btnFind) btnFind.addEventListener("click", findOpcServers);
    if (servers) {
      servers.addEventListener("change", () => {
        const val = servers.value;
        if (val && progId) progId.value = val;
        stopLive();
        invalidateApproval();
      });
    }
    if (progId) {
      progId.addEventListener("input", () => {
        stopLive();
        invalidateApproval();
      });
    }
    if (interval) {
      interval.addEventListener("input", () => {
        stopLive();
        invalidateApproval();
      });
    }
    if (btnAddTag) btnAddTag.addEventListener("click", () => addTag());
    if (btnValAll) btnValAll.addEventListener("click", () => validateTagList());
    if (btnSaveCfg) btnSaveCfg.addEventListener("click", saveConfig);
    if (btnToggle) btnToggle.addEventListener("click", toggleLive);
    if (btnApply) btnApply.addEventListener("click", applyToAgent);
    if (btnLoadAct) {
      btnLoadAct.addEventListener("click", () => {
        if (!activeAgentConfig) return;
        stopLive();
        invalidateApproval();
        const cfgName = el("config-name");
        if (cfgName) cfgName.value = `Ativa - v${activeAgentConfig.version}`;
        if (progId) progId.value = activeAgentConfig.opc_prog_id;
        if (interval) interval.value = activeAgentConfig.update_rate_ms;
        const tagsContainer = el("tags");
        if (tagsContainer) {
          tagsContainer.replaceChildren();
          activeAgentConfig.tags.forEach(t => addTag(t));
        }
        updateTagCountBadge();
        updateButtons();
      });
    }
  }

  function initPiModule() {
    if (el("pi-equipment-select")) {
      el("pi-equipment-select").addEventListener("change", onPiEquipmentSelected);
    }
    if (el("btn-discover-pi-servers")) {
      el("btn-discover-pi-servers").addEventListener("click", discoverPiServers);
    }
    if (el("pi-prog-id")) {
      el("pi-prog-id").addEventListener("change", onPiProgIdChanged);
      el("pi-prog-id").addEventListener("blur", onPiProgIdChanged);
      el("pi-prog-id").addEventListener("keydown", e => {
        if (e.key === "Enter") {
          e.preventDefault();
          onPiProgIdChanged();
        }
      });
    }
    if (el("btn-save-pi-profile")) {
      el("btn-save-pi-profile").addEventListener("click", savePiProfile);
    }
    if (el("pi-point-source")) {
      el("pi-point-source").addEventListener("input", clearProfileError);
    }
    if (el("pi-location1")) {
      el("pi-location1").addEventListener("input", clearProfileError);
    }

    if (el("btn-add-pi-row")) {
      el("btn-add-pi-row").addEventListener("click", addPiRow);
    }
    if (el("btn-delete-pi-row")) {
      el("btn-delete-pi-row").addEventListener("click", deleteSelectedRow);
    }
    if (el("btn-save-pi-sheet")) {
      el("btn-save-pi-sheet").addEventListener("click", savePiSheetChanges);
    }
    if (el("btn-read-now-pi")) {
      el("btn-read-now-pi").addEventListener("click", readNowPiTags);
    }
    const sheetTable = el("pi-spreadsheet-table");
    if (sheetTable) {
      sheetTable.tabIndex = 0;
      sheetTable.addEventListener("paste", handleTabularPaste);
      sheetTable.addEventListener("copy", e => {
        if (!piIsEditing && piSelectionRange) {
          e.preventDefault();
          copySelectionToClipboard();
        }
      });
      sheetTable.addEventListener("cut", e => {
        if (!piIsEditing && piSelectionRange) {
          e.preventDefault();
          cutSelection();
        }
      });
    }

    if (el("btn-refresh-pi-audit")) {
      el("btn-refresh-pi-audit").addEventListener("click", loadPiAudit);
    }
    if (el("btn-cancel-delete-modal")) {
      el("btn-cancel-delete-modal").addEventListener("click", () => {
        deletingMappingId = null;
        el("modal-delete-mapping-confirm").hidden = true;
      });
    }
    if (el("btn-confirm-delete-mapping")) {
      el("btn-confirm-delete-mapping").addEventListener("click", executeDeleteMapping);
    }
    if (el("btn-test-pi-connection")) {
      el("btn-test-pi-connection").addEventListener("click", testPiConnection);
    }
    if (el("btn-cancel-publish-once-modal")) {
      el("btn-cancel-publish-once-modal").addEventListener("click", closePublishOnceModal);
    }
    if (el("btn-confirm-publish-once")) {
      el("btn-confirm-publish-once").addEventListener("click", confirmPublishOnce);
    }
  }

  function initGlobalListeners() {
    const sheetTable = el("pi-spreadsheet-table");

    // Global mouseup and mousemove for selection dragging and fill handle dragging
    document.addEventListener("mouseup", () => {
      if (piIsDraggingHandle) {
        finishFillHandleDrag();
      }
      piIsMouseDown = false;
      piDragAnchor = null;
      piMouseDownPos = null;
      piIsDraggingSelection = false;
    });

    document.addEventListener("mousemove", e => {
      if (piIsDraggingHandle && piDragHandleSourceRange) {
        const domRows = getTableRows();
        if (domRows.length > 0) {
          const lastRow = domRows[domRows.length - 1];
          const rect = lastRow.getBoundingClientRect();
          if (e.clientY > rect.bottom) {
            const rowHeight = rect.height || 30;
            const extra = Math.min(50, Math.max(1, Math.floor((e.clientY - rect.bottom) / rowHeight) + 1));
            const targetR = (domRows.length - 1) + extra;
            while (getTableRows().length <= targetR) {
              addPiRowSilently();
            }
            updateRowIndices();
            updateRowCountBadge();
            onFillHandleHoverRow(targetR);
          }
        }
      }
    });

    // Global keyboard handler when spreadsheet or active cell is focused
    document.addEventListener("keydown", e => {
      if (currentTab !== "pi") return;
      if (!piActiveCell) return;
      const activeEl = document.activeElement;
      const isTableDescendant = activeEl && (activeEl.closest && activeEl.closest("#pi-spreadsheet-table"));
      const isBodyOrTable = !activeEl || activeEl === document.body || activeEl === sheetTable;
      if (isTableDescendant || isBodyOrTable) {
        handleCellKeyDown(e, piActiveCell.rIdx, piActiveCell.cIdx);
      }
    });

    // Window / Page lifecycle events: Auto-stop live monitoring
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopLive();
    });
    window.addEventListener("pagehide", stopLive);
    window.addEventListener("beforeunload", stopLive);
  }

  async function init() {
    try {
      // Step 1: Initialize tab navigation
      initTabs();

      // Step 2: Initialize Equipments & OPC modules
      initEquipmentsModule();
      initOpcModule();

      // Step 3: Initialize PI Integration module with isolation
      try {
        initPiModule();
      } catch (errPi) {
        console.error("Falha ao inicializar listeners do módulo PI:", errPi);
        message(`Aviso na inicialização do módulo PI: ${errPi.message}`, "warning");
      }

      // Step 4: Initialize global input & lifecycle listeners
      initGlobalListeners();

      // Step 5: Core server health & capabilities
      await api("/health");
      const [agentsData, capsData] = await Promise.all([
        api("/api/v1/agents"),
        api("/api/v1/ui-capabilities")
      ]);
      availableAgents = agentsData.agents || [];
      dispatch = capsData.dispatch_available;

      // Step 6: Initial data load and initial tab display
      await loadEquipments();
      addTag();
      switchTab("equipments");

      // Auto-refresh operational states: only while on "pi" tab; no auto-refresh on Equipamentos and OPC
      autoRefreshTimer = setInterval(() => {
        if (currentTab === "pi" && selectedPiEquipment && selectedPiProgId) {
          const activeEl = document.activeElement;
          const isEditingTable = activeEl && activeEl.closest && activeEl.closest("#pi-spreadsheet-table");
          if (!isEditingTable && !piHasPendingChanges) {
            loadPiMappings().catch(() => {});
          }
          loadPiAudit().catch(() => {});
        }
      }, 5000);
    } catch (err) {
      console.error("Erro na inicialização da interface:", err);
      message(`Erro na inicialização da interface: ${err.message}`, "error");
      try {
        switchTab("equipments");
      } catch (_) {}
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
