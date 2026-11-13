import { ActionIcon, Center, Grid, Group, Loader as Spin, Modal, Paper, Select, Stack, Table, Text, ThemeIcon,
         Tooltip } from "@mantine/core";
import { Dropzone } from "@mantine/dropzone";
import { IconEye, IconFileTypePdf, IconPhoto, IconUpload, IconX } from "@tabler/icons-react";
import { useEffect, useState } from "react";
import { ApiError, api } from "../services/api";
import type { Config, Doc, Workspace } from "../types";
import { t, when } from "../utils/text";
import { notifyError, type Act } from "./kit";

/** The Sale Deed and supporting documents, compact: a drop zone (with the document type) and the files on file
 *  (preview). The Sale Deed's details are not keyed here: the agency reports them in its Verification Report. */
export function Documents({ ws, config, act }: { ws: Workspace; config: Config; act: Act }) {
  const id = ws.application.application_id;
  const [kind, setKind] = useState<string>(ws.deed_document_id ? "ENCUMBRANCE_CERTIFICATE" : "SALE_DEED");
  const [preview, setPreview] = useState<Doc | null>(null);
  const kindLabel = (k: string) => config.document_kinds.find((x) => x.id === k)?.label ?? k;
  const kinds = config.document_kinds.filter((k) => !config.agency_kinds.includes(k.id));
  const docs = ws.documents.filter((d) => !config.agency_kinds.includes(d.kind));

  const upload = async (files: File[]) => {
    const file = files[0];
    if (!file) return;
    await act("upload", () => api.upload(id, kind, file), t("docs.uploaded", { kind: kindLabel(kind) }));
  };

  return (
    <Stack gap="md">
      {ws.can.upload && (
        <Grid gutter="sm" align="stretch">
          <Grid.Col span={{ base: 12, sm: 4 }}>
            <Select label={t("docs.kind")} data={kinds.map((k) => ({ value: k.id, label: k.label }))}
                    value={kind} onChange={(v) => setKind(v ?? "SALE_DEED")} allowDeselect={false} />
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 8 }}>
            <Dropzone onDrop={upload} onReject={() => notifyError(new ApiError(415, "FILE_TYPE", t("docs.bad_type")))}
                      maxSize={config.max_mb * 1024 * 1024} accept={config.allowed_types} multiple={false} className="dropzone"
                      data-testid="dropzone">
              <Group gap="sm" wrap="nowrap" style={{ pointerEvents: "none" }}>
                <Dropzone.Accept><ThemeIcon variant="light" color="green"><IconUpload size={18} /></ThemeIcon></Dropzone.Accept>
                <Dropzone.Reject><ThemeIcon variant="light" color="red"><IconX size={18} /></ThemeIcon></Dropzone.Reject>
                <Dropzone.Idle><ThemeIcon variant="gradient"><IconUpload size={18} /></ThemeIcon></Dropzone.Idle>
                <div>
                  <Text size="sm" fw={700}>{t("docs.drop_title", { kind: kindLabel(kind) })}</Text>
                  <Text size="xs" c="dimmed">{t("docs.drop", { mb: config.max_mb })}</Text>
                </div>
              </Group>
            </Dropzone>
          </Grid.Col>
        </Grid>
      )}

      <Grid gutter="md">
        <Grid.Col span={12}>
          <Text size="xs" fw={800} tt="uppercase" c="dimmed" mb={6}>{t("docs.on_file", { n: docs.length })}</Text>
          {docs.length === 0 ? <Paper withBorder p="md"><Text size="sm" c="dimmed">{t("docs.none_text")}</Text></Paper> : (
            <Paper withBorder>
              <Table verticalSpacing={6} highlightOnHover>
                <Table.Tbody>
                  {docs.map((d) => (
                    <Table.Tr key={d.document_id}>
                      <Table.Td w={36}>
                        <ThemeIcon variant="light" size={30} radius="md">
                          {d.content_type === "application/pdf" ? <IconFileTypePdf size={16} /> : <IconPhoto size={16} />}</ThemeIcon></Table.Td>
                      <Table.Td><Text size="sm" fw={700}>{d.kind_label}</Text><Text size="xs" c="dimmed">{d.file_name} · {when(d.uploaded_at)}</Text></Table.Td>
                      <Table.Td w={40}>
                        <Tooltip label={t("docs.view")}><ActionIcon variant="subtle" onClick={() => setPreview(d)} aria-label={t("docs.view")}>
                          <IconEye size={16} /></ActionIcon></Tooltip></Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Paper>
          )}
        </Grid.Col>
      </Grid>

      {preview && <Preview doc={preview} onClose={() => setPreview(null)} />}
    </Stack>
  );
}

export function Preview({ doc, onClose }: { doc: Doc; onClose: () => void }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let made: string | null = null;
    api.fileBlob(doc.document_id).then((b) => { made = URL.createObjectURL(b); setUrl(made); })
      .catch((e) => setError(e instanceof ApiError ? e.message : t("common.failed")));
    return () => { if (made) URL.revokeObjectURL(made); };
  }, [doc.document_id]);
  return (
    <Modal opened onClose={onClose} title={`${doc.kind_label} · ${doc.file_name}`} size="80%" centered>
      {error && <Text c="red">{error}</Text>}
      {!url && !error && <Center h={300}><Spin /></Center>}
      {url && (doc.content_type === "application/pdf"
        ? <iframe className="preview-frame" src={url} title={doc.file_name} />
        : <Center><img src={url} alt={doc.file_name} style={{ maxWidth: "100%" }} /></Center>)}
    </Modal>
  );
}
