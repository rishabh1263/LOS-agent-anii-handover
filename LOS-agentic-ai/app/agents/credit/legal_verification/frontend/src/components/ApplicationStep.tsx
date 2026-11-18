import { Badge, Button, Group, Modal, Paper, RingProgress, Select, SimpleGrid, Stack, Text, TextInput, Textarea } from "@mantine/core";
import { IconBan, IconBuildingCommunity, IconInfoCircle, IconCircleCheckFilled, IconCloudUpload, IconDeviceFloppy, IconFileText, IconLock, IconMapPin,
         IconScale, IconSend, IconSparkles, IconUser } from "@tabler/icons-react";
import { useMemo, useState, type ReactNode } from "react";
import { api, newKey } from "../services/api";
import type { Config, Workspace } from "../types";
import { t } from "../utils/text";
import { Documents } from "./Documents";
import { notifyError, type Act } from "./kit";

const GROUPS: Record<string, { icon: typeof IconUser; tone: string }> = {
  case: { icon: IconFileText, tone: "indigo" }, property: { icon: IconBuildingCommunity, tone: "blue" },
  owner: { icon: IconUser, tone: "violet" }, address: { icon: IconMapPin, tone: "teal" }, identity: { icon: IconScale, tone: "orange" },
};

/** Step 1 -- the Application Details as one form (owner 2026-10-29): the LOS's fields grouped, each group in its own
 *  shade; dropdowns where the field has choices; the read-only values from the LOS shown locked; "Fill sample" on top;
 *  under the form the Sale Deed upload; under that Save (details + legal vendor), Initiate and Waive. */
export function ApplicationStep({ ws, config, act, busy }: { ws: Workspace; config: Config; act: Act; busy: string | null }) {
  const app = ws.application;
  const id = app.application_id;
  const initial = useMemo(() => Object.fromEntries(config.application_fields.map((f) => [f.id, String(app.edit[f.id] ?? "")])),
                          [config.application_fields, app.edit]);
  const [values, setValues] = useState<Record<string, string>>(initial);
  const [vendor, setVendor] = useState(ws.case.agency ?? ws.case.selected_agency ?? config.default_agency ?? "");
  const [filling, setFilling] = useState(false);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [waiving, setWaiving] = useState(false);
  const [reason, setReason] = useState("");
  const editable = ws.can.edit_application;
  const changed = Object.fromEntries(Object.entries(values).filter(([k, v]) => v !== initial[k]));
  const field = (fid: string) => config.application_fields.find((f) => f.id === fid);
  const locked = Object.fromEntries((app.view.find((g) => g.id === "case")?.fields ?? []).map((f) => [f.id, f]));
  const vendorChanged = vendor && vendor !== (ws.case.selected_agency ?? ws.case.agency ?? "");
  const agencyName = (a: string) => config.agencies.find((x) => x.id === a)?.name ?? a;
  // why Initiate is off, in the order the CPA would fix it (null: it is on)
  const initiateWhy = ws.can.send && vendor ? null
    : ws.case.agency ? t("actions.why_sent", { agency: ws.case.agency_name ?? "" })
    : ws.agency_report ? t("actions.why_report")
    : ["DECIDED", "WAIVED"].includes(ws.case.status) ? t("actions.why_closed")
    : !ws.deed_document_id ? t("actions.why_no_deed")
    : !vendor ? t("actions.why_no_vendor")
    : t("actions.why_not_allowed");

  const fill = async () => {
    setFilling(true);
    try {
      const s = (await api.sample(id, "application")).values;
      setValues((v) => ({ ...v, ...Object.fromEntries(Object.entries(s).filter(([k]) => k in v).map(([k, x]) => [k, String(x ?? "")])) }));
    } catch (e) { notifyError(e); } finally { setFilling(false); }
  };
  const save = () => act("save", async () => {
    let next: Workspace | undefined;
    if (Object.keys(changed).length) next = await api.editApplication(id, changed);
    if (vendorChanged && ws.can.save) next = await api.saveVendor(id, vendor);
    return next;
  }, t("actions.saved_all"));

  const formIds = config.application_form.flatMap((g) => g.fields);
  const filled = formIds.filter((fid) => (values[fid] ?? "").trim()).length;
  const pct = formIds.length ? Math.round((100 * filled) / formIds.length) : 0;
  const mark = (fid: string) => values[fid] !== initial[fid] ? <span className="dot-changed" title={t("application.changed")} />
    : (values[fid] ?? "").trim() ? <IconCircleCheckFilled size={15} className="tick-filled" /> : null;

  const input = (fid: string) => {
    const f = field(fid);
    if (!f) return null;
    const common = { key: fid, label: f.label, disabled: !editable, classNames: { input: "soft-input", label: "soft-label" } };
    return f.options
      ? <Select {...common} data={f.options.map((o) => ({ value: o.id, label: o.label }))} value={values[fid] || null} searchable
                onChange={(v) => setValues({ ...values, [fid]: v ?? "" })} placeholder={t("report.choose")} allowDeselect={false}
                leftSection={values[fid] !== initial[fid] ? <span className="dot-changed" title={t("application.changed")} /> : null}
                comboboxProps={{ transitionProps: { transition: "pop", duration: 160 } }} />
      : <TextInput {...common} value={values[fid] ?? ""} onChange={(e) => setValues({ ...values, [fid]: e.currentTarget.value })}
                   description={fid.endsWith("_pan") ? t("application.pan_note") : undefined}
                   rightSection={mark(fid)} />;
  };
  const group = (gid: string, label: string, children: ReactNode, cols = 3) => {
    const g = GROUPS[gid] ?? GROUPS.case;
    const ids = config.application_form.find((x) => x.id === gid)?.fields ?? [];
    const done = ids.filter((fid) => (values[fid] ?? "").trim()).length;
    const n = config.application_form.findIndex((x) => x.id === gid) + 1;
    return (
      <Paper key={gid} className="fcard" radius="lg">
        <div className="fcard-head">
          <span className="fcard-num">{String(n).padStart(2, "0")}</span>
          <span className="fcard-icon"><g.icon size={16} /></span>
          <Text fw={800} size="sm" className="fcard-title">{label}</Text>
          {ids.length > 0 && <span className={`fcard-count${done === ids.length ? " is-full" : ""}`}>{done}/{ids.length}</span>}
        </div>
        <SimpleGrid cols={{ base: 1, sm: 2, lg: cols }} spacing="md" verticalSpacing={10} className="fcard-body">{children}</SimpleGrid>
      </Paper>
    );
  };

  return (
    <Stack gap="sm">
      <div className="form-hero">
        <div className="form-hero-text">
          <Text fw={800} fz={20} c="white" lh={1.2}>{t("sections.application")}</Text>
          <Text size="xs" className="form-hero-note">{t("application.form_note")}</Text>
          <Group gap={8} mt={10}>
            {["applicant_name", "reference_id"].map((fid) => (
              <span key={fid} className="hero-chip"><IconLock size={12} /><i>{locked[fid]?.label ?? fid}</i><b>{locked[fid]?.value || "—"}</b></span>
            ))}
          </Group>
        </div>
        <Group gap="md" wrap="nowrap">
          <div className="hero-ring">
            <RingProgress size={78} thickness={7} roundCaps sections={[{ value: pct, color: "var(--warm)" }]}
                          rootColor="rgba(255,255,255,.18)" label={<Text ta="center" fw={800} size="sm" c="white">{pct}%</Text>} />
            <Text size="10px" fw={700} className="form-hero-note" ta="center">{t("application.progress", { done: filled, total: formIds.length })}</Text>
          </div>
          {config.show_fill_sample && editable && (
            <Button variant="white" color="dark" radius="xl" leftSection={<IconSparkles size={15} />} loading={filling} onClick={fill}
                    className="btn-anim hero-fill">{t("docs.fill_sample")}</Button>
          )}
        </Group>
      </div>
      <div className="form-pair">
        {config.application_form.filter((g) => ["property", "owner"].includes(g.id)).map((g) => group(g.id, g.label, g.fields.map(input), 2))}
      </div>
      {config.application_form.filter((g) => g.id === "address").map((g) => group(g.id, g.label, [
        ...g.fields.map(input),
        <div key="complete" className="complete-address">
          <span className="addr-pin"><IconMapPin size={18} /></span>
          <div><Text size="xs" fw={700} className="soft-label">{t("application_groups.complete_address")}</Text>
            <Text fw={700} size="sm">{app.property_address || "—"}</Text></div>
        </div>,
      ], 4))}
      {config.application_form.filter((g) => g.id === "identity").map((g) => group(g.id, g.label, [
        ...g.fields.map(input),
        <Select key="vendor" label={t("application.vendor_name")} placeholder={t("application.select_agency")}
                data={config.agencies.map((a) => ({ value: a.id, label: a.name }))} value={vendor || null}
                onChange={(v) => setVendor(v ?? "")} disabled={!ws.can.save} allowDeselect={false}
                classNames={{ input: "soft-input is-vendor", label: "soft-label" }} />,
      ], 3))}

      <Paper className="fcard upload-card" radius="lg">
        <div className="fcard-head">
          <span className="fcard-num">{String(config.application_form.length + 1).padStart(2, "0")}</span>
          <span className="fcard-icon"><IconCloudUpload size={16} /></span>
          <Text fw={800} size="sm" className="fcard-title">{t("sections.documents")}</Text>
        </div>
        <div className="fcard-body">
        <Documents ws={ws} config={config} act={act} />
        </div>
      </Paper>

      {ws.case.status !== "WAIVED" && (
        <Paper className="action-dock glass-dock" p="md" radius="xl">
          <Group justify="space-between" wrap="wrap" gap="sm">
            <div>
              <Text fw={800} size="sm">{ws.case.agency ? t("actions.sent_title", { agency: ws.case.agency_name ?? "" }) : t("actions.ready_title")}</Text>
              <Text size="xs" c="dimmed" maw={520}>{ws.case.agency ? t("actions.sent_note", { agency: ws.case.agency_name ?? "" }) : t("actions.note")}</Text>
            </div>
            <Group gap="sm">
              {(Object.keys(changed).length > 0 || vendorChanged) && <Badge color="orange" variant="dot">{t("application.unsaved")}</Badge>}
              <Button size="md" variant="default" leftSection={<IconDeviceFloppy size={17} />} loading={busy === "save"}
                      disabled={!editable && !ws.can.save} onClick={save} className="btn-anim">{t("actions.save")}</Button>
              <Button size="md" variant="gradient" leftSection={<IconSend size={17} />}
                      disabled={!ws.can.send || !vendor} loading={busy === "send"} onClick={() => setConfirm(newKey())} className="btn-anim">
                {t("actions.initiate")}</Button>
              <Button size="md" color="red" variant="light" leftSection={<IconBan size={17} />} disabled={!ws.can.waive}
                      loading={busy === "waive"} onClick={() => setWaiving(true)} className="btn-anim">{t("actions.waive")}</Button>
            </Group>
          </Group>
          {initiateWhy && (
            <Group gap={6} mt={8} className="initiate-why" wrap="nowrap">
              <IconInfoCircle size={15} style={{ flex: "none" }} /><Text size="xs" fw={600}>{initiateWhy}</Text>
            </Group>
          )}
        </Paper>
      )}

      {/* Confirm and send (owner 2026-10-30: "card aur proper aur bada"): a band with the vendor, what is sent, what follows */}
      <Modal opened={!!confirm} onClose={() => setConfirm(null)} centered radius="xl" size="xl" padding={0} withCloseButton={false}
             overlayProps={{ blur: 3, backgroundOpacity: 0.4 }} classNames={{ content: "send-card" }}>
        <div className="send-card-band">
          <Group gap="md" wrap="nowrap">
            <span className="send-card-icon"><IconSend size={28} /></span>
            <div style={{ minWidth: 0 }}>
              <Text fw={800} fz={22} c="white" lh={1.2}>{t("agency.confirm_title")}</Text>
              <Text size="sm" className="send-card-sub">{t("agency.confirm_text", { agency: agencyName(vendor) })}</Text>
            </div>
          </Group>
          <span className="send-card-vendor"><IconScale size={14} />{t("application.vendor_name")}: <b>{agencyName(vendor)}</b></span>
        </div>
        <Stack gap="md" p="xl">
          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
            <div className="send-info"><span className="send-info-icon"><IconUser size={18} /></span>
              <div><Text size="xs" fw={700} c="dimmed" tt="uppercase">{t("agency.confirm_owner_label")}</Text>
                <Text fw={700}>{app.owner_names.join(", ") || "—"}</Text></div></div>
            <div className="send-info"><span className="send-info-icon"><IconMapPin size={18} /></span>
              <div><Text size="xs" fw={700} c="dimmed" tt="uppercase">{t("agency.confirm_property_label")}</Text>
                <Text fw={700}>{app.property_address || "—"}</Text></div></div>
          </SimpleGrid>
          <div className="send-info">
            <span className="send-info-icon"><IconFileText size={18} /></span>
            <div style={{ flex: 1 }}>
              <Text size="xs" fw={700} c="dimmed" tt="uppercase">{t("agency.confirm_docs", { n: ws.documents.length })}</Text>
              <Group gap={6} mt={6}>{ws.documents.map((d) => <Badge key={d.document_id} variant="light" size="lg" radius="sm">{d.kind_label}</Badge>)}</Group>
            </div>
          </div>
          <Group gap={8} wrap="nowrap" className="send-next">
            <IconInfoCircle size={18} style={{ flex: "none" }} /><Text size="sm">{t("agency.confirm_next")}</Text>
          </Group>
          <Group justify="flex-end" gap="sm" mt="xs">
            <Button size="lg" variant="default" onClick={() => setConfirm(null)}>{t("common.cancel")}</Button>
            <Button size="lg" variant="gradient" leftSection={<IconSend size={18} />} loading={busy === "send"} className="btn-anim"
                    onClick={async () => { if (confirm && await act("send", () => api.send(id, vendor, confirm), t("agency.sent_ok"))) setConfirm(null); }}>
              {t("agency.confirm")}</Button>
          </Group>
        </Stack>
      </Modal>
      <Modal opened={waiving} onClose={() => setWaiving(false)} title={t("actions.waive_title")} centered radius="lg">
        <Stack gap="sm">
          <Text size="sm" c="dimmed">{t("actions.waive_note")}</Text>
          <Textarea label={t("actions.waive_reason")} withAsterisk value={reason} onChange={(e) => setReason(e.currentTarget.value)} autoFocus />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setWaiving(false)}>{t("common.cancel")}</Button>
            <Button color="red" leftSection={<IconBan size={15} />} disabled={!reason.trim()} loading={busy === "waive"}
                    onClick={async () => { if (await act("waive", () => api.waive(id, reason), t("actions.waived"))) setWaiving(false); }}>
              {t("actions.waive_confirm")}</Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
