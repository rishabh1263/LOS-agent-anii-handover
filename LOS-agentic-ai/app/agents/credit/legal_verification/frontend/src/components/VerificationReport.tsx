import { ActionIcon, Alert, Badge, Button, Divider, Group, Modal, NumberInput, Paper, SegmentedControl, Select, SimpleGrid, Stack,
         Table, Text, TextInput, Textarea, ThemeIcon } from "@mantine/core";
import { Dropzone } from "@mantine/dropzone";
import { IconBuildingBank, IconCheck, IconDownload, IconMinus, IconX, IconCircleCheck, IconClockHour4, IconEye, IconFileAnalytics,
         IconFileCertificate, IconFileTypePdf, IconPencil, IconPaperclip, IconPlus, IconSend, IconSparkles, IconTrash } from "@tabler/icons-react";
import { useState, type ReactNode } from "react";
import { api, saveBlob } from "../services/api";
import type { Config, Doc, FormField, Workspace } from "../types";
import { Preview } from "./Documents";
import { t, when } from "../utils/text";
import { notifyError, type Act } from "./kit";

type Answers = Record<string, unknown>;
type Row = Record<string, string>;
const ROWS: Record<string, string[]> = { people: ["first", "middle", "last"], chain: ["date", "from_owner", "to_owner", "document"],
                                         searches: ["search_type", "searched_at", "reference", "parameters", "result"] };
const WIDE = new Set(["textarea", "people", "chain", "searches", "table", "yesno", "yesnona"]);
const YN_ICON: Record<string, typeof IconCheck> = { yes: IconCheck, no: IconX, na: IconMinus };
/** a Yes / No / Not applicable answer with its icon */
function YnLabel({ id, text }: { id: string; text: string }) {
  const I = YN_ICON[id];
  return <span className="yn-label">{I && <I size={14} stroke={2.4} />}{text}</span>;
}
const empty = (v: unknown) => v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0);

/** The Verification Report, on the page (not a pop-up): the Leviosa form's groups in their order, a responsive grid,
 *  filled by the agency (Fill sample in the demo) and read by everyone else. */
export function VerificationReport({ ws, config, act }: { ws: Workspace; config: Config; act: Act }) {
  const rep = ws.agency_report;
  const editable = ws.can.submit_report;
  const [answers, setAnswers] = useState<Answers>(() => ({ ...(rep?.answers ?? {}) }));
  const [tried, setTried] = useState(false);
  const [filling, setFilling] = useState(false);
  const [preview, setPreview] = useState<Doc | null>(null);
  const files = ws.documents.filter((d) => config.agency_kinds.includes(d.kind));
  const generated = files.find((d) => d.document_id === rep?.document_id);
  const download = (d: Doc) => act("download", async () => saveBlob(await api.fileBlob(d.document_id), d.file_name),
                                   t("report.downloaded"));
  const attach = async (picked: File[]) => {
    if (picked[0]) await act("upload", () => api.upload(ws.application.application_id, config.agency_kinds[0], picked[0]), t("docs.uploaded", {
      kind: config.document_kinds.find((k) => k.id === config.agency_kinds[0])?.label ?? "" }));
  };
  const set = (k: string, v: unknown) => setAnswers((a) => ({ ...a, [k]: v }));
  const fields = config.form.sections.flatMap((s) => s.fields);
  const missing = fields.filter((f) => f.required && empty(answers[f.id]));
  const fill = async () => {
    setFilling(true);
    try { setAnswers((await api.sample(ws.application.application_id, "report")).values); } catch (e) { notifyError(e); }
    finally { setFilling(false); }
  };
  const submit = async () => {
    setTried(true);
    if (missing.length) return;
    if (await act("report", () => api.submitReport(ws.application.application_id, answers), t("agency.submitted"))) setEditing(false);
  };
  const [editing, setEditing] = useState(!rep);
  const [ask, setAsk] = useState(true);              // the pop-up: generate the agency report now?
  const generate = () => act("generate", () => api.generateReport(ws.application.application_id), t("report.generated_ok"));

  // ONE agency report (owner 2026-10-30): the agency's own file if it attached one, else the report generated from its
  // answers. Only the report here: the rules and the LLM summary live in Rule Results and Final Status.
  const own = [...files].reverse().find((d) => d.document_id !== rep?.document_id);
  const theFile = own ?? generated;
  if (rep && !editing) {
    const a = rep.answers as Record<string, string>;
    return (
      <Stack gap="lg">
        <Paper radius="xl" className="report-card">
          <div className="report-card-band">
            <Group justify="space-between" wrap="wrap" gap="sm">
              <Group gap="md" wrap="nowrap" style={{ minWidth: 0 }}>
                <span className="report-card-icon"><IconFileCertificate size={28} /></span>
                <div style={{ minWidth: 0 }}>
                  <Text size="xs" fw={800} tt="uppercase" lts={1.2} className="report-card-kicker">{t("report.card_title")}</Text>
                  <Text fw={800} fz={21} c="white" lh={1.2}>{ws.case.agency_name ?? rep.agency}</Text>
                  <Text size="xs" className="report-card-meta">
                    {[a.advocate_name, a.report_reference, a.report_date].filter(Boolean).join("  ·  ")}</Text>
                </div>
              </Group>
              <Badge size="lg" variant="white" color="teal" leftSection={<IconCircleCheck size={14} />}>
                {t("agency.report_in", { when: when(rep.submitted_at) })}</Badge>
            </Group>
          </div>
          <div className="report-card-body">
            {a.opinion_text && <blockquote className="report-quote">{a.opinion_text}</blockquote>}
            {theFile ? (
              <Group className="report-file" wrap="wrap" gap="sm">
                <ThemeIcon size={42} radius="md" variant="gradient"><IconFileTypePdf size={22} /></ThemeIcon>
                <div style={{ flex: 1, minWidth: 180 }}>
                  <Text fw={800} size="sm" truncate>{theFile.file_name}</Text>
                  <Text size="xs" c="dimmed">{theFile.uploaded_by} · {when(theFile.uploaded_at)}</Text>
                </div>
                <Button variant="default" leftSection={<IconEye size={15} />} onClick={() => setPreview(theFile)}>{t("docs.view")}</Button>
                <Button variant="gradient" leftSection={<IconDownload size={15} />} onClick={() => download(theFile)} className="btn-anim">
                  {t("report.download")}</Button>
              </Group>
            ) : ws.can.generate_report ? (
              <Group className="report-file generate-card" justify="space-between" wrap="wrap" gap="sm">
                <div style={{ flex: 1, minWidth: 220 }}><Text fw={800} size="sm">{t("report.generate_title")}</Text>
                  <Text size="xs" c="dimmed">{t("report.generate_text")}</Text></div>
                <Button variant="gradient" leftSection={<IconSparkles size={16} />} className="btn-anim" onClick={generate}>
                  {t("report.generate")}</Button>
              </Group>
            ) : <Text size="sm" c="dimmed">{t("report.no_file")}</Text>}
            {/* every answer, section by section, open on the page (owner: "jo fields pehle dikh rahe the") */}
            <Stack gap="md" mt="lg" className="report-answers">
              {config.form.sections.map((s) => (
                <div key={s.id} className="answer-section">
                  <Divider label={<Text fw={800} size="xs" tt="uppercase" lts={0.6} className="sec-label">{s.label}</Text>} labelPosition="left" mb="sm" />
                  <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} spacing="md" verticalSpacing="sm">
                    {s.fields.map((f) => (
                      <div key={f.id} className={WIDE.has(f.type) && !["yesno", "yesnona"].includes(f.type) ? "grid-wide" : undefined}>
                        <ReadOnly field={f} value={rep.answers[f.id]} config={config} />
                      </div>
                    ))}
                  </SimpleGrid>
                </div>
              ))}
            </Stack>
            {editable && (
              <Group justify="flex-end" mt="sm">
                <Button variant="subtle" leftSection={<IconPencil size={15} />} onClick={() => setEditing(true)}>{t("report.edit_answers")}</Button>
              </Group>
            )}
          </div>
        </Paper>
        {preview && <Preview doc={preview} onClose={() => setPreview(null)} />}
        <Modal opened={ask && !theFile && ws.can.generate_report} onClose={() => setAsk(false)} centered radius="xl" size="md"
               withCloseButton={false} classNames={{ content: "gen-pop" }} overlayProps={{ blur: 3, backgroundOpacity: 0.35 }}>
          <Stack align="center" gap="sm" ta="center" p="xs">
            <span className="gen-pop-icon"><IconFileCertificate size={34} /></span>
            <Text fw={800} fz="lg">{t("report.popup_title")}</Text>
            <Text size="sm" c="dimmed">{t("report.popup_text")}</Text>
            <Group mt="xs">
              <Button variant="default" onClick={() => setAsk(false)}>{t("report.popup_later")}</Button>
              <Button variant="gradient" leftSection={<IconSparkles size={16} />} className="btn-anim"
                      onClick={async () => { setAsk(false); await generate(); }}>{t("report.generate")}</Button>
            </Group>
          </Stack>
        </Modal>
      </Stack>
    );
  }

  return (
    <Stack gap="md">
      <Paper withBorder p="sm" className="agency-strip">
        <Group gap="sm" wrap="nowrap">
          <ThemeIcon size={34} radius="md" variant="light"><IconBuildingBank size={18} /></ThemeIcon>
          <div style={{ flex: 1, minWidth: 0 }}>
            <Text fw={800} size="sm">{ws.case.agency_name ?? t("agency.not_sent")}</Text>
            <Text size="xs" c="dimmed">{ws.case.agency ? t("agency.sent", { who: ws.case.sent_by ?? "—", when: when(ws.case.sent_at) })
                                                       : t("agency.not_sent_text")}</Text>
          </div>
          {rep ? <Badge color="green" leftSection={<IconSend size={11} />}>{t("agency.report_in", { when: when(rep.submitted_at) })}</Badge>
               : ws.case.agency ? <Badge color="yellow" leftSection={<IconClockHour4 size={11} />}>{t("agency.awaiting")}</Badge> : null}
        </Group>
      </Paper>

      {ws.case.agency && (
        <Paper withBorder p="sm">
          <Group justify="space-between" mb={files.length || ws.can.attach_report ? 6 : 0}>
            <Group gap={6}><IconPaperclip size={15} /><Text size="xs" fw={800} tt="uppercase" c="dimmed">{t("report.files")}</Text></Group>
            {!files.length && !ws.can.attach_report && <Text size="xs" c="dimmed">{t("report.no_files")}</Text>}
          </Group>
          {files.map((d) => (
            <Group key={d.document_id} gap="sm" wrap="nowrap" py={4}>
              <ThemeIcon variant={d.document_id === rep?.document_id ? "gradient" : "light"} size={28} radius="md">
                {d.document_id === rep?.document_id ? <IconFileAnalytics size={15} /> : <IconFileTypePdf size={15} />}</ThemeIcon>
              <div style={{ flex: 1, minWidth: 0 }}><Group gap={6}><Text size="sm" fw={700}>{d.file_name}</Text>
                {d.document_id === rep?.document_id && <Badge size="xs" variant="light">{t("report.generated")}</Badge>}</Group>
                <Text size="xs" c="dimmed">{d.uploaded_by} · {when(d.uploaded_at)}</Text></div>
              <ActionIcon variant="subtle" onClick={() => setPreview(d)} aria-label={t("docs.view")}><IconEye size={16} /></ActionIcon>
              <ActionIcon variant="subtle" onClick={() => download(d)} aria-label={t("report.download_file")}><IconDownload size={16} /></ActionIcon>
            </Group>
          ))}
          {ws.can.attach_report && (
            <Dropzone onDrop={attach} accept={config.allowed_types} maxSize={config.max_mb * 1024 * 1024} multiple={false}
                      className="dropzone" mt={6}>
              <Group gap="sm" style={{ pointerEvents: "none" }}>
                <ThemeIcon variant="gradient"><IconPaperclip size={16} /></ThemeIcon>
                <Text size="sm" fw={700}>{t("report.attach")}</Text>
              </Group>
            </Dropzone>
          )}
        </Paper>
      )}
      {preview && <Preview doc={preview} onClose={() => setPreview(null)} />}
      {!ws.case.agency && !rep ? null : (!rep && !editable) ? (
        <Alert color="yellow" variant="light">{t("report.awaited")}</Alert>
      ) : (
        <>
          {config.form.sections.map((s) => (
            <div key={s.id}>
              <Divider label={<Text fw={800} size="xs" tt="uppercase" c="sapphire.8" lts={0.6}>{s.label}</Text>} labelPosition="left" mb="sm" />
              <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} spacing="md" verticalSpacing="md">
                {s.fields.map((f) => (
                  <div key={f.id} className={WIDE.has(f.type) && !["yesno", "yesnona"].includes(f.type) ? "grid-wide" : f.type.startsWith("yesno") ? "grid-half" : undefined}>
                    {editable
                      ? <Input field={f} value={answers[f.id]} onChange={(v) => set(f.id, v)} config={config}
                               error={tried && !!f.required && empty(answers[f.id])} />
                      : <ReadOnly field={f} value={answers[f.id]} config={config} />}
                  </div>
                ))}
              </SimpleGrid>
            </div>
          ))}
          {editable && (
            <Group justify="space-between" className="report-actions">
              {config.show_fill_sample
                ? <Button variant="subtle" leftSection={<IconSparkles size={15} />} loading={filling} onClick={fill}>{t("report.fill_sample")}</Button>
                : <span />}
              <Group gap="sm">
                {!tried && <Text c="dimmed" size="xs" maw={340}>{t("report.generated_note")}</Text>}
                {tried && missing.length > 0 && <Text c="red" size="sm">{t("report.missing", { n: missing.length })}</Text>}
                <Button size="md" variant="gradient" leftSection={<IconSend size={16} />} onClick={submit} className="btn-anim">
                  {t("report.submit")}</Button>
              </Group>
            </Group>
          )}
        </>
      )}
    </Stack>
  );
}

function optionLabel(f: FormField, v: unknown) {
  return f.options.find((o) => o.id === v)?.label ?? (empty(v) ? "" : String(v));
}

function Input({ field, value, onChange, config, error }: {
  field: FormField; value: unknown; onChange: (v: unknown) => void; config: Config; error: boolean;
}) {
  const err = error ? t("report.required") : undefined;
  const common = { label: field.label, withAsterisk: !!field.required, error: err };
  switch (field.type) {
    case "textarea": return <Textarea {...common} value={String(value ?? "")} onChange={(e) => onChange(e.currentTarget.value)} />;
    case "date": return <TextInput {...common} type="date" value={String(value ?? "")} onChange={(e) => onChange(e.currentTarget.value)} />;
    case "number": return <NumberInput {...common} value={value === undefined || value === null ? "" : Number(value)} min={0}
                                       onChange={(v) => onChange(v === "" ? null : v)} />;
    case "select": return <Select {...common} data={field.options.map((o) => ({ value: o.id, label: o.label }))} value={(value as string) || null}
                                  onChange={(v) => onChange(v ?? "")} placeholder={t("report.choose")} searchable />;
    case "yesno": case "yesnona":
      return (
        <Stack gap={4}>
          <Text size="sm" fw={500}>{field.label}{field.required && <Text span c="red"> *</Text>}</Text>
          <SegmentedControl value={(value as string) || ""} onChange={onChange} size="sm" radius="xl" color="sapphire"
                            transitionDuration={220} withItemsBorders={false}
                            className={`yn-seg${value ? " is-answered" : ""}${error ? " seg-error" : ""}`}
                            data={field.options.map((o) => ({ value: o.id, label: <YnLabel id={o.id} text={o.label} /> }))} />
        </Stack>
      );
    case "people": case "chain": case "searches":
      return <Rows field={field} value={value} onChange={onChange} config={config} error={err} />;
    case "table":
      return <TableRows field={field} value={value} onChange={onChange} config={config} error={err} />;
    default: return <TextInput {...common} value={String(value ?? "")} onChange={(e) => onChange(e.currentTarget.value)} />;
  }
}

function Rows({ field, value, onChange, config, error }: {
  field: FormField; value: unknown; onChange: (v: unknown) => void; config: Config; error?: string;
}) {
  const cols = ROWS[field.type];
  const rows = (Array.isArray(value) ? value : []) as Row[];
  const set = (i: number, k: string, v: string) => onChange(rows.map((r, j) => (j === i ? { ...r, [k]: v } : r)));
  const list = field.type === "people" ? rows.length ? rows : [{}] : rows;
  return (
    <Stack gap={6}>
      <Text size="sm" fw={500}>{field.label}{field.required && <Text span c="red"> *</Text>}</Text>
      <Paper withBorder p={6}>
        <Stack gap={6}>
          {list.map((r, i) => (
            <Group key={i} gap={6} wrap="nowrap" align="flex-end">
              {field.type === "people" && <Text size="xs" c="dimmed" w={64}>{t("report.owner_n", { n: i + 1 })}</Text>}
              {cols.map((c) => c === "search_type"
                ? <Select key={c} size="xs" style={{ flex: 1 }} placeholder={t(`report.${field.type}.${c}`)} aria-label={t(`report.${field.type}.${c}`)}
                          data={config.form.search_types.map((o) => ({ value: o.id, label: o.label }))} value={r[c] || null}
                          onChange={(v) => set(i, c, v ?? "")} />
                : <TextInput key={c} size="xs" style={{ flex: 1 }} type={c === "date" ? "date" : "text"} value={r[c] ?? ""}
                             placeholder={t(`report.${field.type}.${c}`)} aria-label={t(`report.${field.type}.${c}`)}
                             onChange={(e) => set(i, c, e.currentTarget.value)} />)}
              <ActionIcon variant="subtle" color="gray" onClick={() => onChange(rows.filter((_, j) => j !== i))}
                          aria-label={t(`report.${field.type}.remove`)}><IconTrash size={14} /></ActionIcon>
            </Group>
          ))}
          <Button size="compact-xs" variant="light" leftSection={<IconPlus size={12} />} w="fit-content"
                  onClick={() => onChange([...(field.type === "people" ? list : rows), {}])}>{t(`report.${field.type}.add`)}</Button>
        </Stack>
      </Paper>
      {error && <Text c="red" size="xs">{error}</Text>}
    </Stack>
  );
}

/** A table field: one row per entry, the columns (text / date / dropdown) from config, compact on a small screen. */
function TableRows({ field, value, onChange, config, error }: {
  field: FormField; value: unknown; onChange: (v: unknown) => void; config: Config; error?: string;
}) {
  const rows = (Array.isArray(value) ? value : []) as Row[];
  const set = (i: number, k: string, v: string) => onChange(rows.map((r, j) => (j === i ? { ...r, [k]: v } : r)));
  return (
    <Stack gap={6}>
      <Text size="sm" fw={500}>{field.label}{field.required && <Text span c="red"> *</Text>}</Text>
      <Paper withBorder p={8} className="table-field">
        <Stack gap={8}>
          {rows.map((r, i) => (
            <div key={i} className="table-row" style={{ gridTemplateColumns: `repeat(${field.columns.length}, minmax(0, 1fr)) auto` }}>
              {field.columns.map((c) => c.type === "select"
                ? <Select key={c.id} size="xs" label={i === 0 ? c.label : undefined} aria-label={c.label} placeholder={c.label}
                          data={c.options.map((o) => ({ value: o.id, label: o.label }))} value={r[c.id] || null}
                          onChange={(v) => set(i, c.id, v ?? "")} />
                : <TextInput key={c.id} size="xs" label={i === 0 ? c.label : undefined} aria-label={c.label} placeholder={c.label}
                             type={c.type === "date" ? "date" : "text"} value={r[c.id] ?? ""}
                             onChange={(e) => set(i, c.id, e.currentTarget.value)} />)}
              <ActionIcon variant="subtle" color="gray" onClick={() => onChange(rows.filter((_, j) => j !== i))}
                          aria-label={config.form.table_words.remove} className="table-remove"><IconTrash size={14} /></ActionIcon>
            </div>
          ))}
          <Button size="compact-xs" variant="light" leftSection={<IconPlus size={12} />} w="fit-content"
                  onClick={() => onChange([...rows, {}])}>{config.form.table_words.add}</Button>
        </Stack>
      </Paper>
      {error && <Text c="red" size="xs">{error}</Text>}
    </Stack>
  );
}

function ReadOnly({ field, value, config }: { field: FormField; value: unknown; config: Config }) {
  let shown: ReactNode = null;
  if (field.type === "table") {
    const rows = (value as Row[]) ?? [];
    const cell = (c: FormField["columns"][number], v: string) => c.options.find((o) => o.id === v)?.label ?? (v || "—");
    shown = rows.length ? (
      <Table fz="xs" withTableBorder>
        <Table.Thead><Table.Tr>{field.columns.map((c) => <Table.Th key={c.id}>{c.label}</Table.Th>)}</Table.Tr></Table.Thead>
        <Table.Tbody>{rows.map((r, i) => <Table.Tr key={i}>{field.columns.map((c) => <Table.Td key={c.id}>{cell(c, r[c.id])}</Table.Td>)}</Table.Tr>)}</Table.Tbody>
      </Table>) : null;
  } else if (field.type === "people") shown = ((value as Row[]) ?? []).map((r) => ROWS.people.map((k) => r[k]).filter(Boolean).join(" ")).join(", ");
  else if (field.type === "chain" || field.type === "searches") {
    const rows = (value as Row[]) ?? [];
    shown = rows.length ? (
      <Table fz="xs" withTableBorder>
        <Table.Thead><Table.Tr>{ROWS[field.type].map((c) => <Table.Th key={c}>{t(`report.${field.type}.${c}`)}</Table.Th>)}</Table.Tr></Table.Thead>
        <Table.Tbody>{rows.map((r, i) => <Table.Tr key={i}>{ROWS[field.type].map((c) => <Table.Td key={c}>
          {c === "search_type" ? config.form.search_types.find((s) => s.id === r[c])?.label ?? r[c] : r[c] || "—"}</Table.Td>)}</Table.Tr>)}</Table.Tbody>
      </Table>) : null;
  } else if (field.type.startsWith("yesno") && !empty(value)) {
    const I = YN_ICON[String(value)];
    shown = <span className={`yn-chip yn-${String(value)}`}>{I && <I size={13} stroke={2.6} />}{optionLabel(field, value)}</span>;
  } else if (field.type === "select" || field.type.startsWith("yesno")) shown = optionLabel(field, value);
  else shown = empty(value) ? null : String(value);
  return (
    <Stack gap={2}>
      <Text size="xs" c="dimmed" fw={600}>{field.label}</Text>
      {shown ? <Text size="sm" fw={700} component="div">{shown}</Text> : <Text size="sm" c="gray.5">—</Text>}
    </Stack>
  );
}
