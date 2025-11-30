import { ComponentFixture, TestBed } from '@angular/core/testing';

import { Topnav } from './topnav';

describe('Topnav', () => {
  let component: Topnav;
  let fixture: ComponentFixture<Topnav>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Topnav]
    })
    .compileComponents();

    fixture = TestBed.createComponent(Topnav);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
