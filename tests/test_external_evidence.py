from hydrodynamics import validate_external_evidence, VerticalDatumAttestation, DispatchApprovalEvidence, LeveeCrestEvidence, BenchmarkEvidence

def test_external_evidence_missing_blocks_absolute_case():
    r=validate_external_evidence(absolute_level_required=True); assert r.status=="block" and r.datum_status=="block"

def test_complete_external_evidence_can_verify_contracts():
    d=VerticalDatumAttestation("provider","1985国家高程基准","orthometric","org","2026-01-01","ref")
    a=DispatchApprovalEvidence("approved","reviewer","2026-01-01","v1","ref")
    l=LeveeCrestEvidence("L1","seg",0.,10.,"1985国家高程基准",False,False)
    b=BenchmarkEvidence("B1","obs","water_depth","2026","domain","RMSE",0.1,0.05,"approved")
    r=validate_external_evidence(datum=d,dispatch=a,levee=[l],benchmark=b,absolute_level_required=True); assert r.status=="pass" and r.datum_status=="verified"
