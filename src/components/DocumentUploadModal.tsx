import React, { useCallback, useEffect, useRef, useState } from 'react';
import { UploadCloud, X, File, CheckCircle, AlertTriangle, Loader2, Copy } from 'lucide-react';
import { fetchDocumentStatus, uploadDocument } from '../services/api';
import { DocumentJobStatus } from '../types';

interface DocumentUploadModalProps {
  isOpen: boolean;
  onClose: () => void;
  onUploadSuccess: () => void;
}

type Phase = 'IDLE' | 'UPLOADING' | 'PROCESSING' | 'DONE' | 'DUPLICATE' | 'ERROR';

const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 180_000;

const CLASS_LABEL: Record<string, string> = {
  CREDIT_REPORT: 'Credit report',
  BANK_STATEMENT: 'Bank statement',
  CREDIT_CARD_STATEMENT: 'Credit card statement',
  PAYSTUB: 'Paystub',
  STUDENT_LOAN_STATEMENT: 'Student loan statement',
  TAX_DOCUMENT: 'Tax document',
  MISCELLANEOUS_FINANCIAL: 'Financial document',
};

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export const DocumentUploadModal: React.FC<DocumentUploadModalProps> = ({ isOpen, onClose, onUploadSuccess }) => {
  const [dragActive, setDragActive] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>('IDLE');
  const [errorMsg, setErrorMsg] = useState('');
  const [detectedClass, setDetectedClass] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const cancelled = useRef(false);

  useEffect(() => {
    cancelled.current = false;
    return () => {
      cancelled.current = true;
    };
  }, []);

  const reset = useCallback(() => {
    setFile(null);
    setPhase('IDLE');
    setErrorMsg('');
    setDetectedClass(null);
  }, []);

  const handleDrag = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === 'dragenter' || e.type === 'dragover') setDragActive(true);
    else if (e.type === 'dragleave') setDragActive(false);
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);
    if (e.dataTransfer.files && e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
  }, []);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    e.preventDefault();
    if (e.target.files && e.target.files[0]) setFile(e.target.files[0]);
  };

  const pollJob = async (savedAs: string): Promise<DocumentJobStatus> => {
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    for (;;) {
      if (cancelled.current) throw new Error('Cancelled.');
      const job = await fetchDocumentStatus(savedAs);
      if (job.status === 'DONE' || job.status === 'DUPLICATE' || job.status === 'FAILED') return job;
      if (Date.now() > deadline) throw new Error('Extraction is taking too long. Check back later; it keeps running on the engine.');
      await sleep(POLL_INTERVAL_MS);
    }
  };

  const handleUpload = async () => {
    if (!file) return;
    setPhase('UPLOADING');
    setErrorMsg('');
    const receipt = await uploadDocument(file);
    if (!receipt.success || !receipt.saved_as) {
      setPhase('ERROR');
      setErrorMsg(receipt.message);
      return;
    }

    setPhase('PROCESSING');
    try {
      const job = await pollJob(receipt.saved_as);
      if (job.status === 'FAILED') {
        setPhase('ERROR');
        setErrorMsg(job.error || 'The document could not be read.');
        return;
      }
      setDetectedClass(job.document_class ?? null);
      setPhase(job.status === 'DUPLICATE' ? 'DUPLICATE' : 'DONE');
      onUploadSuccess();
    } catch (err: unknown) {
      setPhase('ERROR');
      setErrorMsg(err instanceof Error ? err.message : 'Extraction status could not be read.');
    }
  };

  const close = () => {
    reset();
    onClose();
  };

  if (!isOpen) return null;

  const busy = phase === 'UPLOADING' || phase === 'PROCESSING';
  const finished = phase === 'DONE' || phase === 'DUPLICATE';

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
      <div className="bg-[#0e1422] border border-gray-800 rounded-xl shadow-2xl w-full max-w-lg overflow-hidden flex flex-col">
        <div className="flex items-center justify-between p-4 border-b border-gray-800 bg-[#0b101c]">
          <h3 className="font-semibold text-gray-100 font-mono flex items-center">
            <UploadCloud className="w-5 h-5 mr-2 text-indigo-400" />
            Upload document
          </h3>
          <button onClick={close} disabled={busy} className="text-gray-500 hover:text-gray-300 transition-colors disabled:opacity-40" aria-label="Close">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-5">
          {phase === 'IDLE' || phase === 'ERROR' ? (
            <div
              onDragEnter={handleDrag}
              onDragLeave={handleDrag}
              onDragOver={handleDrag}
              onDrop={handleDrop}
              onClick={() => inputRef.current?.click()}
              className={`border-2 border-dashed rounded-xl p-8 flex flex-col items-center justify-center cursor-pointer transition-colors ${
                dragActive ? 'border-indigo-500 bg-indigo-500/5' : 'border-gray-800 bg-[#111827] hover:border-gray-700 hover:bg-[#151c28]'
              }`}
            >
              <input ref={inputRef} type="file" className="hidden" accept=".pdf,.png,.jpg,.jpeg" onChange={handleChange} />
              {!file ? (
                <>
                  <div className="w-12 h-12 rounded-full bg-gray-900 flex items-center justify-center mb-4">
                    <UploadCloud className="w-6 h-6 text-gray-500" />
                  </div>
                  <p className="text-sm text-gray-300 font-mono mb-1">Drop a file or tap to choose</p>
                  <p className="text-xs text-gray-600 font-mono text-center">
                    PDF, PNG or JPG up to 20 MB. The document type is detected automatically.
                  </p>
                </>
              ) : (
                <div className="flex items-center gap-3 w-full max-w-xs bg-gray-900 p-3 rounded-lg border border-gray-800">
                  <File className="w-8 h-8 text-indigo-400 shrink-0" />
                  <div className="flex-1 overflow-hidden">
                    <p className="text-sm text-gray-300 font-mono truncate">{file.name}</p>
                    <p className="text-xs text-gray-500 font-mono">{(file.size / 1024 / 1024).toFixed(2)} MB</p>
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setFile(null);
                    }}
                    className="text-gray-500 hover:text-rose-400 p-1"
                    aria-label="Remove file"
                  >
                    <X className="w-4 h-4" />
                  </button>
                </div>
              )}
            </div>
          ) : (
            <div className="bg-[#111827] border border-gray-800 rounded-xl p-8 flex flex-col items-center justify-center min-h-[200px] text-center">
              {phase === 'UPLOADING' && (
                <>
                  <Loader2 className="w-10 h-10 text-indigo-500 animate-spin mb-4" />
                  <p className="text-sm font-mono text-indigo-300">Uploading securely…</p>
                </>
              )}
              {phase === 'PROCESSING' && (
                <>
                  <Loader2 className="w-10 h-10 text-emerald-500 animate-spin mb-4" />
                  <p className="text-sm font-mono text-emerald-300">Reading and classifying the document…</p>
                  <p className="text-xs font-mono text-gray-500 mt-2">This can take up to a minute. Trading is unaffected.</p>
                </>
              )}
              {phase === 'DONE' && (
                <>
                  <CheckCircle className="w-10 h-10 text-emerald-500 mb-4" />
                  <p className="text-sm font-mono text-emerald-300">Recorded as {detectedClass ? CLASS_LABEL[detectedClass] ?? detectedClass : 'a financial document'}</p>
                  <p className="text-xs font-mono text-gray-500 mt-2">Record-keeping only. Position sizing is not changed.</p>
                </>
              )}
              {phase === 'DUPLICATE' && (
                <>
                  <Copy className="w-10 h-10 text-amber-400 mb-4" />
                  <p className="text-sm font-mono text-amber-300">This exact file was already ingested.</p>
                  <p className="text-xs font-mono text-gray-500 mt-2">Nothing was changed.</p>
                </>
              )}
            </div>
          )}

          {errorMsg && (
            <div className="mt-4 p-3 bg-rose-500/10 border border-rose-500/20 rounded-lg flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 text-rose-400 mt-0.5 shrink-0" />
              <p className="text-xs text-rose-300 font-mono">{errorMsg}</p>
            </div>
          )}
        </div>

        <div className="p-4 border-t border-gray-800 bg-[#0b101c] flex justify-end gap-3">
          <button onClick={close} disabled={busy} className="px-4 py-2 text-xs font-mono text-gray-400 hover:text-gray-200 disabled:opacity-50">
            {finished ? 'Close' : 'Cancel'}
          </button>
          {!finished && (
            <button
              onClick={handleUpload}
              disabled={!file || busy}
              className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:bg-indigo-900 disabled:text-indigo-400 text-white text-xs font-mono rounded transition-colors"
            >
              {busy ? 'Working…' : 'Upload'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
};
