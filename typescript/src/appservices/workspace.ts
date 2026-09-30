/**
 * ProjectWorkspace — a `.vowdo/` project directory bound to real persistence.
 *
 * NAMESPACED STORAGE (migration rule): TS workspaces carry marker
 * schemaVersion "2" + tool "vowdo-agent-ts". The Python reference writes
 * schemaVersion "1" + tool "vouch-agent" (historical); each implementation refuses the
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

export const WORKSPACE_DIRNAME = ".vowdo";
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
    readonly vowdoDir: string,
  ) {}

  /** Create `<projectDir>/.vowdo` and persist the ProjectSpec. Refuses overwrite. */
  static create(projectDir: string, spec: ProjectSpecData): ProjectWorkspace {
    Deno.mkdirSync(projectDir, { recursive: true });
    projectDir = Deno.realPathSync(projectDir);
    // Rename boundary on INIT too: never silently initialize beside (or
    // over) a pre-rename `.vouch` workspace — preserve it and provide guidance.
    const legacy = legacyWorkspaceGuidance(projectDir);
    if (legacy !== null) throw new ContractError(legacy);
    const vowdoDir = joinPath(projectDir, WORKSPACE_DIRNAME);
    if (isDirectory(vowdoDir) || isFile(vowdoDir)) {
      throw new ContractError(
        `${vowdoDir} already exists; refusing to re-init — open it instead (or move it away deliberately)`,
      );
    }
    Deno.mkdirSync(vowdoDir, { recursive: true });
    Deno.mkdirSync(joinPath(vowdoDir, "adapter-workspace"), { recursive: true });
    const marker: WorkspaceMarker = {
      schemaVersion: WORKSPACE_VERSION,
      tool: TOOL_ID,
      toolVersion: VERSION,
      projectId: spec.projectId,
      storage: "ts-1",
    };
    Deno.writeTextFile(
      joinPath(vowdoDir, WORKSPACE_FILE),
      JSON.stringify(marker, null, 2) + "\n",
    );
    const workspace = new ProjectWorkspace(projectDir, vowdoDir);
    workspace.guardPersistencePaths();
    workspace.specValue = spec;
    workspace.store.save(KIND_PROJECT, spec.projectId, spec);
    return workspace;
  }

  /** Open an existing TS workspace, validating every version marker. */
  static open(projectDir: string): ProjectWorkspace {
    const abs = Deno.realPathSync(projectDir);
    const vowdoDir = joinPath(abs, WORKSPACE_DIRNAME);
    // Persistence paths must be real entries inside the project directory —
    // a symlinked workspace directory or marker would redirect every later
    // DB/artifact write outside the user-selected root (review follow-up).
    if (isSymlink(vowdoDir)) {
      throw new ContractError(
        `${vowdoDir} is a symbolic link; refusing to open a workspace through it`,
      );
    }
    if (!isDirectory(vowdoDir)) {
      // Rename boundary: detect PRE-RENAME workspaces (`.vouch`) and refuse
      // with an actionable path — never silently initialize over old state.
      const legacyMessage = legacyWorkspaceGuidance(abs);
      if (legacyMessage !== null) throw new ContractError(legacyMessage);
      throw new ContractError(
        `${abs} is not a Vowdo TS project (no ${WORKSPACE_DIRNAME} directory); ` +
          `run 'vowdo init' first (Python workspaces need 'vowdo import-python')`,
      );
    }
    const markerPath = joinPath(vowdoDir, WORKSPACE_FILE);
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
        `${abs} is not a Vowdo TS project (no ${WORKSPACE_DIRNAME}/${WORKSPACE_FILE}); ` +
          `run 'vowdo init' first (Python workspaces need 'vowdo import-python')`,
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
      const tool = String(marker["tool"] ?? "");
      if (tool === "vouch-agent-ts") {
        // A `.vowdo` directory carrying a pre-rename marker is equally
        // refused: old sealed run state is never silently resumed under the
        // renamed tool (no migration exists, by reviewed scope decision).
        throw new ContractError(
          `workspace at ${vowdoDir} was written by the pre-rename tool 'vouch-agent-ts'; ` +
            `it is preserved untouched — use the matching historical release for it, verify ` +
            `its exports read-only, and 'vowdo init' a NEW directory for new work`,
        );
      }
      throw new ContractError(
        `workspace at ${vowdoDir} was written by ${JSON.stringify(marker["tool"])} ` +
          `(marker schemaVersion ${JSON.stringify(marker["schemaVersion"])}); this tool ` +
          `(${TOOL_ID}, schema ${WORKSPACE_VERSION}) refuses to open it — fail closed. ` +
          `Python 0.1.0rc1 workspaces: use 'vowdo import-python' into a new directory`,
      );
    }
    const workspace = new ProjectWorkspace(abs, vowdoDir);
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
        `workspace at ${this.vowdoDir} has no project record; refusing to open (re-init into a new directory)`,
      );
    }
    if (ids.length > 1) {
      throw new ContractError(
        `workspace at ${this.vowdoDir} holds ${ids.length} project records (${
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
      this.storeValue = new MetadataStore(joinPath(this.vowdoDir, META_DB));
    }
    return this.storeValue;
  }

  get artifacts(): ArtifactStore {
    if (this.artifactsValue === null) {
      this.artifactsValue = new ArtifactStore(this.vowdoDir);
    }
    return this.artifactsValue;
  }

  get journal(): Journal {
    if (this.journalValue === null) {
      this.journalValue = new Journal(joinPath(this.vowdoDir, JOURNAL_DB));
    }
    return this.journalValue;
  }

  get ledger(): BudgetLedger {
    if (this.ledgerValue === null) {
      this.ledgerValue = new BudgetLedger(
        joinPath(this.vowdoDir, BUDGET_DB),
        this.spec.budget.totalUsdCap,
      );
    }
    return this.ledgerValue;
  }

  get adapterWorkspace(): string {
    return joinPath(this.vowdoDir, "adapter-workspace");
  }

  workspaceInfo(): WorkspaceMarker {
    const marker = JSON.parse(Deno.readTextFileSync(joinPath(this.vowdoDir, WORKSPACE_FILE)));
    if (!isPlainObject(marker)) throw new ContractError("workspace marker is not an object");
    return marker as unknown as WorkspaceMarker;
  }

  /** The SQLite stores and marker must be real entries under .vowdo — a
   * pre-created symlink at any of these paths would redirect persistence
   * outside the project; fail closed instead. */
  guardPersistencePaths(): void {
    for (const name of [META_DB, JOURNAL_DB, BUDGET_DB, "adapter-workspace"]) {
      const path = joinPath(this.vowdoDir, name);
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

/**
 * Guidance for pre-rename `.vouch` directories found where no `.vowdo`
 * exists. Old workspaces FAIL CLOSED with actionable next steps — there is
 * deliberately NO migration (rewriting sealed run state across a rename
 * needs its own reviewed data-migration design): keep the old workspace
 * with its matching historical release, verify its exports read-only, and
 * initialize a distinct NEW Vowdo workspace for new work. Python
 * workspaces go through the reviewed `vowdo import-python` reader.
 * Returns null when nothing legacy is present.
 */
export function legacyWorkspaceGuidance(projectDir: string): string | null {
  const legacyMarker = joinPath(projectDir, ".vouch");
  const markerPath = joinPath(legacyMarker, WORKSPACE_FILE);
  if (!isFile(markerPath)) return null;
  try {
    const marker = JSON.parse(Deno.readTextFileSync(markerPath));
    if (isPlainObject(marker)) {
      const tool = String(marker["tool"] ?? "");
      if (tool === "vouch-agent-ts") {
        return `${projectDir} holds a workspace from the pre-rename release (vouch-agent-ts, ` +
          `.vouch). It is preserved untouched and is NOT migrated or resumed by this tool: ` +
          `keep it and use the matching historical release for it, inspect/export its ` +
          `results read-only ('vowdo verify-export' accepts historical exports), and run ` +
          `'vowdo init' in a NEW directory for new work`;
      }
      if (tool === "vouch-agent") {
        return `${projectDir} holds a PYTHON vouch-agent workspace in .vouch; ` +
          `run 'vowdo import-python --from ${projectDir} --to <new-project-dir>' instead`;
      }
    }
  } catch {
    // unreadable marker: fall through to the generic guidance below
  }
  return `${projectDir} holds an unrecognized legacy .vouch directory; move it aside ` +
    `before initializing a Vowdo workspace here`;
}

export function joinPath(dir: string, name: string): string {
  return dir.endsWith("/") ? `${dir}${name}` : `${dir}/${name}`;
}
