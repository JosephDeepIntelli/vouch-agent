/**
 * ProjectWorkspace — a `.vouch/` project directory bound to real persistence.
 *
 * NAMESPACED STORAGE (migration rule): TS workspaces carry marker
 * schemaVersion "2" + tool "vouch-agent-ts". The Python reference writes
 * schemaVersion "1" + tool "vouch-agent"; each implementation refuses the
 * other's marker, so no incompatible implementation ever writes a live DB
 * of the other. Cross-implementation movement is ONLY the explicit,
 * source-read-only `import-python` path (see appservices/import_python.ts).
 *
 * Open is fail closed: missing marker, unknown schema version/tool,
 * unreadable project record, or a budget cap that disagrees with the
 * ProjectSpec all refuse with a clear error.
 */

import { isPlainObject } from "../contracts/canonical.ts";
import { isDirectory, isFile, isSymlink } from "../contracts/fsutil.ts";
import { ContractError, TOOL_ID } from "../contracts/common.ts";
import { type ProjectSpecData, specFromDict } from "../contracts/project.ts";
import { ArtifactStore } from "../storage/artifacts.ts";
import { BudgetLedger } from "../storage/budget.ts";
import { Journal } from "../storage/journal.ts";
import { MetadataStore } from "../storage/store.ts";
import { VERSION } from "../version.ts";

export const WORKSPACE_DIRNAME = ".vouch";
export const WORKSPACE_VERSION = "2";
export const WORKSPACE_FILE = "workspace.json";

export const META_DB = "meta.sqlite";
export const JOURNAL_DB = "journal.sqlite";
export const BUDGET_DB = "budget.sqlite";

export const KIND_PROJECT = "project";

export interface WorkspaceMarker {
  schemaVersion: string;
  tool: string;
  toolVersion: string;
  projectId: string;
  storage: string;
}

export class ProjectWorkspace {
  private specValue: ProjectSpecData | null = null;
  private storeValue: MetadataStore | null = null;
  private artifactsValue: ArtifactStore | null = null;
  private journalValue: Journal | null = null;
  private ledgerValue: BudgetLedger | null = null;

  private constructor(
    readonly projectDir: string,
    readonly vouchDir: string,
  ) {}

  /** Create `<projectDir>/.vouch` and persist the ProjectSpec. Refuses overwrite. */
  static create(projectDir: string, spec: ProjectSpecData): ProjectWorkspace {
    Deno.mkdirSync(projectDir, { recursive: true });
    projectDir = Deno.realPathSync(projectDir);
    const vouchDir = joinPath(projectDir, WORKSPACE_DIRNAME);
    if (isDirectory(vouchDir) || isFile(vouchDir)) {
      throw new ContractError(
        `${vouchDir} already exists; refusing to re-init — open it instead (or move it away deliberately)`,
      );
    }
    Deno.mkdirSync(vouchDir, { recursive: true });
    Deno.mkdirSync(joinPath(vouchDir, "adapter-workspace"), { recursive: true });
    const marker: WorkspaceMarker = {
      schemaVersion: WORKSPACE_VERSION,
      tool: TOOL_ID,
      toolVersion: VERSION,
      projectId: spec.projectId,
      storage: "ts-1",
    };
    Deno.writeTextFile(
      joinPath(vouchDir, WORKSPACE_FILE),
      JSON.stringify(marker, null, 2) + "\n",
    );
    const workspace = new ProjectWorkspace(projectDir, vouchDir);
    workspace.guardPersistencePaths();
    workspace.specValue = spec;
    workspace.store.save(KIND_PROJECT, spec.projectId, spec);
    return workspace;
  }

  /** Open an existing TS workspace, validating every version marker. */
  static open(projectDir: string): ProjectWorkspace {
    const abs = Deno.realPathSync(projectDir);
    const vouchDir = joinPath(abs, WORKSPACE_DIRNAME);
    // Persistence paths must be real entries inside the project directory —
    // a symlinked workspace directory or marker would redirect every later
    // DB/artifact write outside the user-selected root (review follow-up).
    if (isSymlink(vouchDir)) {
      throw new ContractError(
        `${vouchDir} is a symbolic link; refusing to open a workspace through it`,
      );
    }
    if (!isDirectory(vouchDir)) {
      throw new ContractError(
        `${abs} is not a vouch TS project (no ${WORKSPACE_DIRNAME} directory); ` +
          `run 'vouch init' first (Python workspaces need 'vouch import-python')`,
      );
    }
    const markerPath = joinPath(vouchDir, WORKSPACE_FILE);
    if (isSymlink(markerPath)) {
      throw new ContractError(
        `workspace marker ${markerPath} is a symbolic link; refusing to read through it`,
      );
    }
    let markerText: string;
    try {
      markerText = Deno.readTextFileSync(markerPath);
    } catch {
      throw new ContractError(
        `${abs} is not a vouch TS project (no ${WORKSPACE_DIRNAME}/${WORKSPACE_FILE}); ` +
          `run 'vouch init' first (Python workspaces need 'vouch import-python')`,
      );
    }
    let marker: unknown;
    try {
      marker = JSON.parse(markerText);
    } catch (exc) {
      throw new ContractError(`unreadable workspace marker ${markerPath}: ${exc}`);
    }
    if (!isPlainObject(marker)) {
      throw new ContractError(`workspace marker ${markerPath} is not an object`);
    }
    if (marker["schemaVersion"] !== WORKSPACE_VERSION || marker["tool"] !== TOOL_ID) {
      throw new ContractError(
        `workspace at ${vouchDir} was written by ${JSON.stringify(marker["tool"])} ` +
          `(marker schemaVersion ${JSON.stringify(marker["schemaVersion"])}); this tool ` +
          `(${TOOL_ID}, schema ${WORKSPACE_VERSION}) refuses to open it — fail closed. ` +
          `Python 0.1.0rc1 workspaces: use 'vouch import-python' into a new directory`,
      );
    }
    const workspace = new ProjectWorkspace(abs, vouchDir);
    workspace.guardPersistencePaths();
    workspace.specValue = workspace.loadSpec();
    // Opening the ledger re-validates the persisted cap against the spec.
    void workspace.ledger;
    return workspace;
  }

  private loadSpec(): ProjectSpecData {
    const ids = this.store.listIds(KIND_PROJECT);
    if (ids.length === 0) {
      throw new ContractError(
        `workspace at ${this.vouchDir} has no project record; refusing to open (re-init into a new directory)`,
      );
    }
    if (ids.length > 1) {
      throw new ContractError(
        `workspace at ${this.vouchDir} holds ${ids.length} project records (${
          JSON.stringify(ids)
        }); ` +
          `a workspace owns exactly one project — fail closed`,
      );
    }
    const data = this.store.load(KIND_PROJECT, ids[0]);
    if (data === null) throw new ContractError("project record vanished");
    return specFromDict(data);
  }

  get spec(): ProjectSpecData {
    if (this.specValue === null) throw new ContractError("workspace not initialized");
    return this.specValue;
  }

  get store(): MetadataStore {
    if (this.storeValue === null) {
      this.storeValue = new MetadataStore(joinPath(this.vouchDir, META_DB));
    }
    return this.storeValue;
  }

  get artifacts(): ArtifactStore {
    if (this.artifactsValue === null) {
      this.artifactsValue = new ArtifactStore(this.vouchDir);
    }
    return this.artifactsValue;
  }

  get journal(): Journal {
    if (this.journalValue === null) {
      this.journalValue = new Journal(joinPath(this.vouchDir, JOURNAL_DB));
    }
    return this.journalValue;
  }

  get ledger(): BudgetLedger {
    if (this.ledgerValue === null) {
      this.ledgerValue = new BudgetLedger(
        joinPath(this.vouchDir, BUDGET_DB),
        this.spec.budget.totalUsdCap,
      );
    }
    return this.ledgerValue;
  }

  get adapterWorkspace(): string {
    return joinPath(this.vouchDir, "adapter-workspace");
  }

  workspaceInfo(): WorkspaceMarker {
    const marker = JSON.parse(Deno.readTextFileSync(joinPath(this.vouchDir, WORKSPACE_FILE)));
    if (!isPlainObject(marker)) throw new ContractError("workspace marker is not an object");
    return marker as unknown as WorkspaceMarker;
  }

  /** The SQLite stores and marker must be real entries under .vouch — a
   * pre-created symlink at any of these paths would redirect persistence
   * outside the project; fail closed instead. */
  guardPersistencePaths(): void {
    for (const name of [META_DB, JOURNAL_DB, BUDGET_DB, "adapter-workspace"]) {
      const path = joinPath(this.vouchDir, name);
      if (isSymlink(path)) {
        throw new ContractError(
          `${path} is a symbolic link; refusing to use it as workspace persistence — ` +
            `writes must stay inside the project directory`,
        );
      }
    }
  }

  close(): void {
    if (this.storeValue !== null) {
      this.storeValue.close();
      this.storeValue = null;
    }
  }
}

export function joinPath(dir: string, name: string): string {
  return dir.endsWith("/") ? `${dir}${name}` : `${dir}/${name}`;
}
