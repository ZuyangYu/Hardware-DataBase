import { useCallback, useEffect, useState } from 'react';

import { api, apiDownload } from '../../api/client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input, Label, Textarea } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';

type Skill = {
  skill_ref: string;
  version: string;
  approval_state: string;
  department_id: string;
  execution_bundle_ref: string;
  bundle_invocation_available: boolean;
  package_archive_digest?: string;
  package_files?: string[];
  package_review_flags?: string[];
  package_build_state?: string;
};
type Bundle = {
  bundle_ref: string;
  bundle_digest: string;
  profile_ref: string;
  approval_state: string;
  catalog_source: string;
  network_policy_ref: string;
  required_capabilities: string[];
  metadata?: { image_digest?: string };
};

export type SkillsPanelViewProps = {
  skills: Skill[];
  bundles: Bundle[];
  busy: boolean;
  skillRef: string;
  version: string;
  content: string;
  departmentId: string;
  binding: Record<string, string>;
  onSkillRef: (value: string) => void;
  onVersion: (value: string) => void;
  onContent: (value: string) => void;
  onDepartmentId: (value: string) => void;
  onBinding: (ref: string, bundleRef: string) => void;
  onRegister: () => void;
  onBind: (skill: Skill) => void;
  onState: (skill: Skill, action: string) => void;
  onRefresh: () => void;
  packageFile?: File | null;
  inputSchema?: string;
  onPackageFile?: (file: File | null) => void;
  onInputSchema?: (value: string) => void;
  onUploadPackage?: () => void;
  onRetryBuild?: (skill: Skill) => void;
  onBundleState?: (bundle: Bundle, action: string) => void;
  onDownloadSource?: (skill: Skill) => void;
  pipIndex?: string;
  pipAllowedHosts?: string;
  onPipIndex?: (value: string) => void;
  onSavePipIndex?: () => void;
};

export function SkillsPanelView({ skills, bundles, busy, skillRef, version, content, departmentId,
  binding, onSkillRef, onVersion, onContent, onDepartmentId, onBinding,
  onRegister, onBind, onState, onRefresh, packageFile, inputSchema, onPackageFile,
  onInputSchema, onUploadPackage, onRetryBuild, onBundleState, onDownloadSource,
  pipIndex, pipAllowedHosts, onPipIndex, onSavePipIndex }: SkillsPanelViewProps) {
  return (
    <section data-admin-skills className="mx-[48px] mb-[32px] rounded-[16px] bg-white p-[18px] shadow-[0_2px_10px_rgba(0,0,0,0.05)] max-[900px]:mx-[16px]">
      <div className="flex items-center justify-between gap-[8px]">
        <h2 className="text-[16px] font-semibold">Skill 上传与审核</h2>
        <Button variant="outline" onClick={onRefresh}>刷新</Button>
      </div>
      <p className="mt-[6px] text-[12px] text-[#858b9c]">
        管理员上传 ZIP 后，由独立 Runner 使用固定离线模板构建镜像；先审核源文件与构建结果，再启用。
      </p>
      <div className="mt-[14px] grid gap-[10px] md:grid-cols-2">
        <div><Label htmlFor="skill-ref">Skill 引用</Label><Input id="skill-ref" value={skillRef} onChange={(e) => onSkillRef(e.target.value)} placeholder="skill/report" /></div>
        <div><Label htmlFor="skill-version">版本</Label><Input id="skill-version" value={version} onChange={(e) => onVersion(e.target.value)} /></div>
        <div><Label htmlFor="skill-department">可见部门 ID（执行型 Skill 必填；留空仅文本）</Label><Input id="skill-department" value={departmentId} onChange={(e) => onDepartmentId(e.target.value)} /></div>
        <div className="md:col-span-2"><Label htmlFor="skill-content">文本内容</Label><Textarea id="skill-content" value={content} onChange={(e) => onContent(e.target.value)} /></div>
      </div>
      <Button className="mt-[10px]" disabled={busy || !skillRef.trim() || !content.trim()} onClick={onRegister}>登记草稿</Button>
      {onUploadPackage && <div className="mt-[14px] rounded-[12px] border border-[#eceef3] p-[12px] text-[12px]">
        <strong>上传执行型 Skill</strong> · ZIP 根目录须含 SKILL.md 与 entrypoint.py；脚本读取 /work/in/parameters.json，产物写入 /work/out。可选 requirements.lock（每行固定版本及 SHA-256，构建时从已批准的 HTTPS 源安装 wheel）。
        {onSavePipIndex && <div className="mt-[8px]">
          <Label htmlFor="skill-pip-index">管理员配置 pip 镜像源（仅限运维允许的主机：{pipAllowedHosts || '未配置'}）</Label>
          <div className="flex gap-[8px]"><Input id="skill-pip-index" value={pipIndex ?? ''}
            onChange={(event) => onPipIndex?.(event.target.value)} placeholder="https://pypi.org/simple/" />
            <Button variant="outline" disabled={busy} onClick={onSavePipIndex}>保存依赖源</Button></div>
        </div>}
        <div className="mt-[8px]"><Label htmlFor="skill-package">Skill ZIP</Label>
          <Input id="skill-package" type="file" accept=".zip" onChange={(event) => onPackageFile?.(event.target.files?.[0] ?? null)} /></div>
        <div className="mt-[8px]"><Label htmlFor="skill-schema">参数 JSON Schema</Label>
          <Textarea id="skill-schema" className="font-mono" value={inputSchema ?? ''} onChange={(event) => onInputSchema?.(event.target.value)} /></div>
        <Button className="mt-[8px]" disabled={busy || !packageFile || !skillRef.trim() || !departmentId.trim()} onClick={onUploadPackage}>上传并自动构建</Button>
      </div>}
      <div className="mt-[16px] grid gap-[10px]">
        {skills.map((skill) => (
          <div key={skill.skill_ref} className="rounded-[12px] border border-[#eceef3] p-[12px]">
            <div className="flex flex-wrap items-center gap-[8px] text-[13px]">
              <span className="font-medium">{skill.skill_ref} · {skill.version}</span>
              <Badge>{skill.approval_state}</Badge>
              {skill.execution_bundle_ref && <span>固定任务：{skill.execution_bundle_ref}</span>}
              {skill.department_id && <span>部门 {skill.department_id}</span>}
              {skill.package_build_state && <Badge>构建：{skill.package_build_state}</Badge>}
            </div>
            {skill.package_archive_digest && <div className="mt-[6px] break-all text-[11px] text-[#464c5e]">
              源包 SHA-256：{skill.package_archive_digest}<br />文件：{skill.package_files?.join(', ')}<br />
              审查提示：{skill.package_review_flags?.join(', ') || '无'}
            </div>}
            <div className="mt-[8px] flex flex-wrap items-center gap-[8px]">
              {skill.package_archive_digest && <Button variant="outline" disabled={busy} onClick={() => onDownloadSource?.(skill)}>下载源包审核</Button>}
              {skill.package_build_state === 'uploaded' && <Button variant="outline" disabled={busy} onClick={() => onRetryBuild?.(skill)}>重试构建</Button>}
              {skill.approval_state === 'draft' && (
                <>
                  {!skill.package_archive_digest && <select aria-label={`${skill.skill_ref} 固定任务`} value={binding[skill.skill_ref] ?? ''}
                    onChange={(e) => onBinding(skill.skill_ref, e.target.value)}
                    className="h-[36px] rounded-[8px] border border-[#eceef3] px-[8px] text-[12px]">
                    <option value="">仅文本 / 选择固定任务</option>
                    {bundles.filter((item) => item.approval_state === 'enabled' && item.catalog_source === 'curated' && !item.network_policy_ref).map((item) => <option key={item.bundle_ref} value={item.bundle_ref}>{item.bundle_ref}</option>)}
                  </select>}
                  {!skill.package_archive_digest && <Button variant="outline" disabled={busy || !binding[skill.skill_ref] || !skill.department_id} onClick={() => onBind(skill)}>绑定固定任务</Button>}
                  <Button disabled={busy || (!!skill.package_archive_digest && skill.package_build_state !== 'built')} onClick={() => onState(skill, 'review')}>审核</Button>
                </>
              )}
              {skill.approval_state === 'reviewed' && <Button disabled={busy} onClick={() => onState(skill, 'tested')}>标记测试通过</Button>}
              {skill.approval_state === 'tested' && <Button disabled={busy} onClick={() => onState(skill, 'enable')}>启用</Button>}
              {skill.approval_state !== 'revoked' && <Button variant="outline" disabled={busy} onClick={() => onState(skill, 'revoke')}>撤销</Button>}
            </div>
          </div>
        ))}
      </div>
      {onBundleState && bundles.filter((bundle) => bundle.bundle_ref.startsWith('bundle/skill-')).map((bundle) => (
        <div key={bundle.bundle_ref} className="mt-[8px] rounded-[12px] border border-[#eceef3] p-[12px] text-[12px]">
          <div className="break-all">构建产物：{bundle.bundle_ref} · SHA-256 {bundle.bundle_digest} · {bundle.approval_state}</div>
          {bundle.metadata?.image_digest && <div className="break-all">镜像 ID：{bundle.metadata.image_digest}</div>}
          <div className="mt-[8px] flex gap-[8px]">
            {bundle.approval_state === 'draft' && <Button onClick={() => onBundleState(bundle, 'review')} disabled={busy}>审核构建结果</Button>}
            {bundle.approval_state === 'reviewed' && <Button onClick={() => onBundleState(bundle, 'tested')} disabled={busy}>标记测试通过</Button>}
            {bundle.approval_state === 'tested' && <Button onClick={() => onBundleState(bundle, 'enable')} disabled={busy}>启用构建结果</Button>}
            {bundle.approval_state === 'enabled' && <Button variant="outline" onClick={() => onBundleState(bundle, 'revoke')} disabled={busy}>撤销构建结果</Button>}
          </div>
        </div>
      ))}
    </section>
  );
}

export default function SkillsPanel() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [bundles, setBundles] = useState<Bundle[]>([]);
  const [busy, setBusy] = useState(false);
  const [skillRef, setSkillRef] = useState('');
  const [version, setVersion] = useState('1.0.0');
  const [content, setContent] = useState('');
  const [departmentId, setDepartmentId] = useState('');
  const [binding, setBinding] = useState<Record<string, string>>({});
  const [packageFile, setPackageFile] = useState<File | null>(null);
  const [inputSchema, setInputSchema] = useState('{"type":"object","properties":{},"additionalProperties":false}');
  const [pipIndex, setPipIndex] = useState('');
  const [pipAllowedHosts, setPipAllowedHosts] = useState('');

  const load = useCallback(async () => {
    try {
      const [skillResult, bundleResult, pipResult] = await Promise.all([
        api.get<{ items: Skill[] }>('/api/v1/sandbox/skills'),
        api.get<{ items: Bundle[] }>('/api/v1/sandbox/bundles'),
        api.get<{ index_url: string; allowed_hosts: string }>('/api/v1/sandbox/skill-dependencies'),
      ]);
      setSkills(skillResult.items);
      setBundles(bundleResult.items);
      setPipIndex(pipResult.index_url);
      setPipAllowedHosts(pipResult.allowed_hosts);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : 'Skill 目录读取失败');
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  function act(action: () => Promise<unknown>, message: string) {
    setBusy(true);
    void action().then(() => { notify.success(message); void load(); })
      .catch((error) => { notify.error(error instanceof Error ? error.message : '操作失败'); void load(); })
      .finally(() => setBusy(false));
  }

  async function encodePackage(file: File): Promise<string> {
    if (file.size > 8 * 1024 * 1024) throw new Error('Skill ZIP 超过 8 MiB');
    const data = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result ?? '').split(',')[1] ?? '');
      reader.onerror = () => reject(new Error('无法读取 Skill ZIP'));
      reader.readAsDataURL(file);
    });
    return data;
  }

  const buildPayload = () => ({
    profile_ref: 'profile/dev-l1', base_bundle_ref: 'bundle/report',
    input_schema: JSON.parse(inputSchema) as Record<string, unknown>,
  });

  return <SkillsPanelView skills={skills} bundles={bundles} busy={busy}
    skillRef={skillRef} version={version} content={content} departmentId={departmentId} binding={binding}
    onSkillRef={setSkillRef} onVersion={setVersion} onContent={setContent} onDepartmentId={setDepartmentId}
    onBinding={(ref, value) => setBinding((current) => ({ ...current, [ref]: value }))}
    packageFile={packageFile} inputSchema={inputSchema} onPackageFile={setPackageFile} onInputSchema={setInputSchema}
    pipIndex={pipIndex} pipAllowedHosts={pipAllowedHosts} onPipIndex={setPipIndex}
    onSavePipIndex={() => act(() => api.put('/api/v1/sandbox/skill-dependencies', { index_url: pipIndex.trim() }), '依赖源已更新')}
    onUploadPackage={() => act(async () => {
      if (!packageFile) throw new Error('请选择 Skill ZIP');
      const payload = buildPayload();
      await api.post('/api/v1/sandbox/skills/packages', {
        skill_ref: skillRef.trim(), version: version.trim(), department_id: departmentId.trim(),
        package_base64: await encodePackage(packageFile), input_schema: payload.input_schema,
      });
    }, 'Skill 源包已保存并构建，请审核构建产物')}
    onRetryBuild={(skill) => act(async () => api.post(`/api/v1/sandbox/skills/${encodeURIComponent(skill.skill_ref)}/build`, buildPayload()), '构建完成')}
    onDownloadSource={(skill) => act(() => apiDownload.blob(`/api/v1/sandbox/skills/${encodeURIComponent(skill.skill_ref)}/source`, 'skill-source.zip'), '源包下载已开始')}
    onBundleState={(bundle, action) => act(() => api.post(`/api/v1/sandbox/bundles/${encodeURIComponent(bundle.bundle_ref)}/${action}`), '构建结果状态已更新')}
    onRegister={() => act(() => api.post('/api/v1/sandbox/skills', {
      skill_ref: skillRef.trim(), version: version.trim(), content, department_id: departmentId.trim(),
    }), 'Skill 草稿已登记')}
    onBind={(skill) => {
      const bundle = bundles.find((item) => item.bundle_ref === binding[skill.skill_ref] && item.approval_state === 'enabled');
      if (bundle) act(() => api.post(`/api/v1/sandbox/skills/${encodeURIComponent(skill.skill_ref)}/bind-execution`, {
        bundle_ref: bundle.bundle_ref, bundle_digest: bundle.bundle_digest, profile_ref: bundle.profile_ref,
      }), '固定任务绑定完成，请审核 Skill');
    }}
    onState={(skill, action) => act(() => api.post(`/api/v1/sandbox/skills/${encodeURIComponent(skill.skill_ref)}/${action}`), `Skill ${action} 完成`)}
    onRefresh={() => void load()}
  />;
}
