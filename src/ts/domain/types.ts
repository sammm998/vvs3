/**
 * Domain types mirroring the analysis engine's canonical JSON.
 *
 * These are the *contract* between the Python engine and the TypeScript
 * orchestration layer.  `src/ts/test/schema.test.ts` validates a real engine
 * output against them, so a change on either side that breaks the contract
 * fails the build rather than surfacing in the UI.
 */

export type IdentityState =
  | "CONFIRMED"
  | "HIGH_CONFIDENCE"
  | "AMBIGUOUS"
  | "INSUFFICIENT"
  | "UNRESOLVED";

export type ScaleState = "RESOLVED" | "SCALE_UNKNOWN" | "SCALE_AMBIGUOUS";

export interface Confidence {
  readonly geometry?: number;
  readonly text?: number;
  readonly association?: number;
  readonly topology?: number;
  readonly dimension?: number;
  readonly vertical?: number;
  readonly overall: number;
}

export interface Provenance {
  readonly stage: string;
  readonly rule: string;
  readonly sourceObjectIds: readonly string[];
  readonly inputs: readonly string[];
  readonly notes: readonly string[];
}

export type BBox = readonly [number, number, number, number];
export type Point = readonly [number, number];

export interface GlyphCandidate {
  readonly glyphId: string;
  readonly page: number;
  readonly bbox: BBox;
  readonly character: string | null;
  readonly alternatives: readonly (readonly [string, number])[];
  readonly confidence: number;
  readonly state: IdentityState;
  readonly reasons: readonly string[];
  readonly sourceObjectIds: readonly string[];
  readonly provenance: Provenance;
}

export interface Designation {
  readonly designationId: string;
  readonly page: number;
  readonly designation: string;
  readonly bbox: BBox;
  readonly role: string;
  readonly isLegend: boolean;
  readonly diameterMm: number | null;
  readonly state: IdentityState;
  readonly reasons: readonly string[];
  readonly confidence: Confidence;
  readonly glyphs: readonly string[];
  readonly sourceObjects: readonly string[];
  readonly associatedPhysicalPipeIds: readonly string[];
  readonly provenance: Provenance;
}

export interface PipeCandidate {
  readonly candidateId: string;
  readonly page: number;
  readonly centerline: readonly Point[];
  readonly style: "double_line" | "single_line";
  readonly widthPt: number | null;
  readonly lengthPt: number;
  readonly accepted: boolean;
  readonly rejectionReason: string | null;
  readonly confidence: Confidence;
  readonly sourceObjectIds: readonly string[];
  readonly provenance: Provenance;
}

export interface PipeRun {
  readonly pipeRunId: string;
  readonly page: number;
  readonly centerline: readonly Point[];
  readonly lengthPt: number;
  readonly widthPt: number | null;
  readonly direction: string;
  readonly state: IdentityState;
  readonly reasons: readonly string[];
  readonly provenance: Provenance;
}

export interface PhysicalPipe {
  readonly physicalPipeId: string;
  readonly page: number;
  readonly pipeRunIds: readonly string[];
  readonly geometry: readonly (readonly Point[])[];
  readonly lengthPt: number;
  readonly horizontalLengthM: number | null;
  readonly verticalLengthM: number | null;
  readonly totalLengthM: number | null;
  readonly diameterMm: number | null;
  readonly designation: string | null;
  readonly designationIds: readonly string[];
  readonly verticalIds: readonly string[];
  readonly identityState: IdentityState;
  readonly reasons: readonly string[];
  readonly confidence: Confidence;
  readonly provenance: Provenance;
}

export interface VerticalSegment {
  readonly verticalId: string;
  readonly page: number;
  readonly point: Point;
  readonly attachedRunIds: readonly string[];
  readonly fromElevationM: number | null;
  readonly toElevationM: number | null;
  readonly lengthM: number | null;
  readonly state: IdentityState;
  readonly reasons: readonly string[];
}

export interface QuantityRow {
  readonly designation: string | null;
  readonly diameterMm: number | null;
  readonly horizontalM: number | null;
  readonly verticalM: number | null;
  readonly totalM: number | null;
  readonly pipeCount: number;
  readonly physicalPipeIds: readonly string[];
  readonly state: IdentityState;
  readonly reasons: readonly string[];
  readonly confidence: Confidence;
}

export interface ScaleReport {
  readonly state: ScaleState;
  readonly metresPerPoint: number | null;
  readonly ratioDenominator: number | null;
  readonly sources: readonly (readonly [string, number])[];
  readonly reasons: readonly string[];
}

export interface DrawingInfo {
  readonly file: string;
  readonly pdfSha256: string;
  readonly pages: readonly {
    readonly page: number;
    readonly width: number;
    readonly height: number;
    readonly rotation: number;
  }[];
  readonly vectorObjectCount: number;
  readonly textSpanCount: number;
  readonly excludedAnnotationObjects: number;
}

export interface AnalysisReport {
  readonly schema: "vvs-pipe/analysis/1";
  readonly drawing: DrawingInfo;
  readonly forensicsDigest: string;
  readonly glyphs: readonly GlyphCandidate[];
  readonly designations: readonly Designation[];
  readonly pipeCandidates: readonly PipeCandidate[];
  readonly pipeRuns: readonly PipeRun[];
  readonly physicalPipes: readonly PhysicalPipe[];
  readonly verticals: readonly VerticalSegment[];
  readonly quantities: readonly QuantityRow[];
  readonly scale: readonly ScaleReport[];
  readonly diagnostics: {
    readonly reconciliation: {
      readonly ok: boolean;
      readonly problems: readonly string[];
    };
    readonly pages: readonly unknown[];
  };
  readonly determinism: {
    readonly canonicalDigest: string;
    readonly quantitiesDigest: string;
    readonly physicalPipesDigest: string;
  };
  readonly blind: {
    readonly facitUsedDuringDetection: boolean;
    readonly mode: string;
  };
}

export type JobState = "queued" | "running" | "succeeded" | "failed";

export interface JobProgress {
  readonly stage: string;
  readonly at: string;
}

export interface Job {
  readonly id: string;
  readonly fileName: string;
  readonly state: JobState;
  readonly createdAt: string;
  readonly finishedAt: string | null;
  readonly progress: readonly JobProgress[];
  readonly error: string | null;
  readonly artifacts: Readonly<Record<string, string>>;
}
