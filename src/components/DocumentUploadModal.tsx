import React, { useState, useCallback, useRef } from 'react';
import { UploadCloud, X, File, CheckCircle, AlertTriangle, Loader2 } from 'lucide-react';
import { uploadDocument } from '../services/api';

interface DocumentUploadModalProps {
  isOpen: boolean;
  onClose: () => void;
  onUploadSuccess: () => void;
}

export const DocumentUploadModal: React.FC<DocumentUploadModalProps> = ({ isOpen, onClose, onUploadSuccess }) => {
  const [dragActive, setDragActive] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [status, setStatus] = useState<'IDLE' | 'UPLOADING' | 'PROCESSING' | 'SUCCESS' | 'ERROR'>('IDLE');
  const [errorMsg, setErrorMsg] = useState<string>('');
  const inputRef = useRef<HTMLInputElement>(null);

  const handleDrag = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === "dragenter" || e.type === "dragover") {
      setDragActive(true);
    } else if (e.type === "dragleave") {
      setDragActive(false);
    }
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      setFile(e.dataTransfer.files[0]);
    }
  }, []);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    e.preventDefault();
    if (e.target.files && e.target.files[0]) {
      setFile(e.target.files[0]);
    }
  };

  const handleUpload = async () => {
    if (!file) return;
    setStatus('UPLOADING');
    setErrorMsg('');
    
    // Slight artificial delay to show upload phase before switching to background processing
    await new Promise(r => setTimeout(r, 800));
    setStatus('PROCESSING');

    const result = await uploadDocument(file);
    if (result.success) {
      setStatus('SUCCESS');
      setTimeout(() => {
        onUploadSuccess();
        setFile(null);
        setStatus('IDLE');
      }, 2000);
    } else {
      setStatus('ERROR');
      setErrorMsg(result.message);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
      <div className="bg-[#0e1422] border border-gray-800 rounded-xl shadow-2xl w-full max-w-lg overflow-hidden flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-gray-800 bg-[#0b101c]">
          <h3 className="font-semibold text-gray-100 font-mono flex items-center">
            <UploadCloud className="w-5 h-5 mr-2 text-indigo-400" />
            Ingest Intelligence Document
          </h3>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300 transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-6">
          {/* Drag & Drop Zone */}
          {status === 'IDLE' || status === 'ERROR' ? (
            <div 
              onDragEnter={handleDrag}
              onDragLeave={handleDrag}
              onDragOver={handleDrag}
              onDrop={handleDrop}
              onClick={() => inputRef.current?.click()}
              className={`border-2 border-dashed rounded-xl p-8 flex flex-col items-center justify-center cursor-pointer transition-colors ${dragActive ? 'border-indigo-500 bg-indigo-500/5' : 'border-gray-800 bg-[#111827] hover:border-gray-700 hover:bg-[#151c28]'}`}
            >
              <input ref={inputRef} type="file" className="hidden" accept=".pdf,.png,.jpg,.jpeg" onChange={handleChange} />
              
              {!file ? (
                <>
                  <div className="w-12 h-12 rounded-full bg-gray-900 flex items-center justify-center mb-4">
                    <UploadCloud className="w-6 h-6 text-gray-500" />
                  </div>
                  <p className="text-sm text-gray-300 font-mono mb-1">Drag & Drop file to upload</p>
                  <p className="text-xs text-gray-600 font-mono">Supported: PDF, PNG, JPG (AES-256 Secured)</p>
                </>
              ) : (
                <div className="flex items-center space-x-3 w-full max-w-xs bg-gray-900 p-3 rounded-lg border border-gray-800">
                  <File className="w-8 h-8 text-indigo-400 flex-shrink-0" />
                  <div className="flex-1 overflow-hidden">
                    <p className="text-sm text-gray-300 font-mono truncate">{file.name}</p>
                    <p className="text-xs text-gray-500 font-mono">{(file.size / 1024 / 1024).toFixed(2)} MB</p>
                  </div>
                  <button onClick={(e) => { e.stopPropagation(); setFile(null); }} className="text-gray-500 hover:text-rose-400 p-1">
                    <X className="w-4 h-4" />
                  </button>
                </div>
              )}
            </div>
          ) : (
            <div className="bg-[#111827] border border-gray-800 rounded-xl p-8 flex flex-col items-center justify-center min-h-[200px]">
              {status === 'UPLOADING' && (
                <>
                  <Loader2 className="w-10 h-10 text-indigo-500 animate-spin mb-4" />
                  <p className="text-sm font-mono text-indigo-300">Vaulting locally to data/vault...</p>
                </>
              )}
              {status === 'PROCESSING' && (
                <>
                  <Loader2 className="w-10 h-10 text-emerald-500 animate-spin mb-4" />
                  <p className="text-sm font-mono text-emerald-300">Vertex AI Gemini Multimodal Extraction...</p>
                  <p className="text-xs font-mono text-gray-500 mt-2">Computing net collateral invariant</p>
                </>
              )}
              {status === 'SUCCESS' && (
                <>
                  <CheckCircle className="w-10 h-10 text-emerald-500 mb-4" />
                  <p className="text-sm font-mono text-emerald-300">Extraction Complete</p>
                </>
              )}
            </div>
          )}

          {errorMsg && (
            <div className="mt-4 p-3 bg-rose-500/10 border border-rose-500/20 rounded-lg flex items-start space-x-2">
              <AlertTriangle className="w-4 h-4 text-rose-400 mt-0.5 flex-shrink-0" />
              <p className="text-xs text-rose-300 font-mono">{errorMsg}</p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="p-4 border-t border-gray-800 bg-[#0b101c] flex justify-end space-x-3">
          <button 
            onClick={onClose}
            disabled={status === 'UPLOADING' || status === 'PROCESSING'}
            className="px-4 py-2 text-xs font-mono text-gray-400 hover:text-gray-200 disabled:opacity-50"
          >
            Cancel
          </button>
          <button 
            onClick={handleUpload}
            disabled={!file || status === 'UPLOADING' || status === 'PROCESSING' || status === 'SUCCESS'}
            className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:bg-indigo-900 disabled:text-indigo-400 text-white text-xs font-mono rounded transition-colors"
          >
            {status === 'UPLOADING' || status === 'PROCESSING' ? 'Processing...' : 'Engage Parsing Daemon'}
          </button>
        </div>
      </div>
    </div>
  );
};
